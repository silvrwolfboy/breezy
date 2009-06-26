# Copyright (C) 2008, 2009 Canonical Ltd
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Repository formats using CHK inventories and groupcompress compression."""

import time

from bzrlib import (
    bzrdir,
    chk_map,
    chk_serializer,
    debug,
    errors,
    index as _mod_index,
    inventory,
    knit,
    osutils,
    pack,
    remote,
    repository,
    revision as _mod_revision,
    trace,
    ui,
    )
from bzrlib.btree_index import (
    BTreeGraphIndex,
    BTreeBuilder,
    )
from bzrlib.index import GraphIndex, GraphIndexBuilder
from bzrlib.groupcompress import (
    _GCGraphIndex,
    GroupCompressVersionedFiles,
    )
from bzrlib.repofmt.pack_repo import (
    Pack,
    NewPack,
    KnitPackRepository,
    KnitPackStreamSource,
    PackRootCommitBuilder,
    RepositoryPackCollection,
    RepositoryFormatPack,
    ResumedPack,
    Packer,
    )


class GCPack(NewPack):

    def __init__(self, pack_collection, upload_suffix='', file_mode=None):
        """Create a NewPack instance.

        :param pack_collection: A PackCollection into which this is being
            inserted.
        :param upload_suffix: An optional suffix to be given to any temporary
            files created during the pack creation. e.g '.autopack'
        :param file_mode: An optional file mode to create the new files with.
        """
        # replaced from NewPack to:
        # - change inventory reference list length to 1
        # - change texts reference lists to 1
        # TODO: patch this to be parameterised

        # The relative locations of the packs are constrained, but all are
        # passed in because the caller has them, so as to avoid object churn.
        index_builder_class = pack_collection._index_builder_class
        # from brisbane-core
        if pack_collection.chk_index is not None:
            chk_index = index_builder_class(reference_lists=0)
        else:
            chk_index = None
        Pack.__init__(self,
            # Revisions: parents list, no text compression.
            index_builder_class(reference_lists=1),
            # Inventory: We want to map compression only, but currently the
            # knit code hasn't been updated enough to understand that, so we
            # have a regular 2-list index giving parents and compression
            # source.
            index_builder_class(reference_lists=1),
            # Texts: per file graph, for all fileids - so one reference list
            # and two elements in the key tuple.
            index_builder_class(reference_lists=1, key_elements=2),
            # Signatures: Just blobs to store, no compression, no parents
            # listing.
            index_builder_class(reference_lists=0),
            # CHK based storage - just blobs, no compression or parents.
            chk_index=chk_index
            )
        self._pack_collection = pack_collection
        # When we make readonly indices, we need this.
        self.index_class = pack_collection._index_class
        # where should the new pack be opened
        self.upload_transport = pack_collection._upload_transport
        # where are indices written out to
        self.index_transport = pack_collection._index_transport
        # where is the pack renamed to when it is finished?
        self.pack_transport = pack_collection._pack_transport
        # What file mode to upload the pack and indices with.
        self._file_mode = file_mode
        # tracks the content written to the .pack file.
        self._hash = osutils.md5()
        # a four-tuple with the length in bytes of the indices, once the pack
        # is finalised. (rev, inv, text, sigs)
        self.index_sizes = None
        # How much data to cache when writing packs. Note that this is not
        # synchronised with reads, because it's not in the transport layer, so
        # is not safe unless the client knows it won't be reading from the pack
        # under creation.
        self._cache_limit = 0
        # the temporary pack file name.
        self.random_name = osutils.rand_chars(20) + upload_suffix
        # when was this pack started ?
        self.start_time = time.time()
        # open an output stream for the data added to the pack.
        self.write_stream = self.upload_transport.open_write_stream(
            self.random_name, mode=self._file_mode)
        if 'pack' in debug.debug_flags:
            trace.mutter('%s: create_pack: pack stream open: %s%s t+%6.3fs',
                time.ctime(), self.upload_transport.base, self.random_name,
                time.time() - self.start_time)
        # A list of byte sequences to be written to the new pack, and the
        # aggregate size of them.  Stored as a list rather than separate
        # variables so that the _write_data closure below can update them.
        self._buffer = [[], 0]
        # create a callable for adding data
        #
        # robertc says- this is a closure rather than a method on the object
        # so that the variables are locals, and faster than accessing object
        # members.
        def _write_data(bytes, flush=False, _buffer=self._buffer,
            _write=self.write_stream.write, _update=self._hash.update):
            _buffer[0].append(bytes)
            _buffer[1] += len(bytes)
            # buffer cap
            if _buffer[1] > self._cache_limit or flush:
                bytes = ''.join(_buffer[0])
                _write(bytes)
                _update(bytes)
                _buffer[:] = [[], 0]
        # expose this on self, for the occasion when clients want to add data.
        self._write_data = _write_data
        # a pack writer object to serialise pack records.
        self._writer = pack.ContainerWriter(self._write_data)
        self._writer.begin()
        # what state is the pack in? (open, finished, aborted)
        self._state = 'open'

    def _check_references(self):
        """Make sure our external references are present.

        Packs are allowed to have deltas whose base is not in the pack, but it
        must be present somewhere in this collection.  It is not allowed to
        have deltas based on a fallback repository.
        (See <https://bugs.launchpad.net/bzr/+bug/288751>)
        """
        # Groupcompress packs don't have any external references, arguably CHK
        # pages have external references, but we cannot 'cheaply' determine
        # them without actually walking all of the chk pages.


class ResumedGCPack(ResumedPack):

    def _check_references(self):
        """Make sure our external compression parents are present."""
        # See GCPack._check_references for why this is empty

    def _get_external_refs(self, index):
        # GC repositories don't have compression parents external to a given
        # pack file
        return set()


class GCCHKPacker(Packer):
    """This class understand what it takes to collect a GCCHK repo."""

    def __init__(self, pack_collection, packs, suffix, revision_ids=None,
                 reload_func=None):
        super(GCCHKPacker, self).__init__(pack_collection, packs, suffix,
                                          revision_ids=revision_ids,
                                          reload_func=reload_func)
        self._pack_collection = pack_collection
        # ATM, We only support this for GCCHK repositories
        if pack_collection.chk_index is None:
            raise AssertionError('pack_collection.chk_index should not be None')
        self._gather_text_refs = False
        self._chk_id_roots = []
        self._chk_p_id_roots = []
        self._text_refs = None
        # set by .pack() if self.revision_ids is not None
        self.revision_keys = None

    def _get_progress_stream(self, source_vf, keys, message, pb):
        def pb_stream():
            substream = source_vf.get_record_stream(keys, 'groupcompress', True)
            for idx, record in enumerate(substream):
                if pb is not None:
                    pb.update(message, idx + 1, len(keys))
                yield record
        return pb_stream()

    def _get_filtered_inv_stream(self, source_vf, keys, message, pb=None):
        """Filter the texts of inventories, to find the chk pages."""
        total_keys = len(keys)
        def _filtered_inv_stream():
            id_roots_set = set()
            p_id_roots_set = set()
            stream = source_vf.get_record_stream(keys, 'groupcompress', True)
            for idx, record in enumerate(stream):
                bytes = record.get_bytes_as('fulltext')
                chk_inv = inventory.CHKInventory.deserialise(None, bytes,
                                                             record.key)
                if pb is not None:
                    pb.update('inv', idx, total_keys)
                key = chk_inv.id_to_entry.key()
                if key not in id_roots_set:
                    self._chk_id_roots.append(key)
                    id_roots_set.add(key)
                p_id_map = chk_inv.parent_id_basename_to_file_id
                if p_id_map is None:
                    raise AssertionError('Parent id -> file_id map not set')
                key = p_id_map.key()
                if key not in p_id_roots_set:
                    p_id_roots_set.add(key)
                    self._chk_p_id_roots.append(key)
                yield record
            # We have finished processing all of the inventory records, we
            # don't need these sets anymore
            id_roots_set.clear()
            p_id_roots_set.clear()
        return _filtered_inv_stream()

    def _get_chk_streams(self, source_vf, keys, pb=None):
        # We want to stream the keys from 'id_roots', and things they
        # reference, and then stream things from p_id_roots and things they
        # reference, and then any remaining keys that we didn't get to.

        # We also group referenced texts together, so if one root references a
        # text with prefix 'a', and another root references a node with prefix
        # 'a', we want to yield those nodes before we yield the nodes for 'b'
        # This keeps 'similar' nodes together.

        # Note: We probably actually want multiple streams here, to help the
        #       client understand that the different levels won't compress well
        #       against each other.
        #       Test the difference between using one Group per level, and
        #       using 1 Group per prefix. (so '' (root) would get a group, then
        #       all the references to search-key 'a' would get a group, etc.)
        total_keys = len(keys)
        remaining_keys = set(keys)
        counter = [0]
        if self._gather_text_refs:
            bytes_to_info = inventory.CHKInventory._bytes_to_utf8name_key
            self._text_refs = set()
        def _get_referenced_stream(root_keys, parse_leaf_nodes=False):
            cur_keys = root_keys
            while cur_keys:
                keys_by_search_prefix = {}
                remaining_keys.difference_update(cur_keys)
                next_keys = set()
                def handle_internal_node(node):
                    for prefix, value in node._items.iteritems():
                        # We don't want to request the same key twice, and we
                        # want to order it by the first time it is seen.
                        # Even further, we don't want to request a key which is
                        # not in this group of pack files (it should be in the
                        # repo, but it doesn't have to be in the group being
                        # packed.)
                        # TODO: consider how to treat externally referenced chk
                        #       pages as 'external_references' so that we
                        #       always fill them in for stacked branches
                        if value not in next_keys and value in remaining_keys:
                            keys_by_search_prefix.setdefault(prefix,
                                []).append(value)
                            next_keys.add(value)
                def handle_leaf_node(node):
                    # Store is None, because we know we have a LeafNode, and we
                    # just want its entries
                    for file_id, bytes in node.iteritems(None):
                        name_utf8, file_id, revision_id = bytes_to_info(bytes)
                        self._text_refs.add((file_id, revision_id))
                def next_stream():
                    stream = source_vf.get_record_stream(cur_keys,
                                                         'as-requested', True)
                    for record in stream:
                        bytes = record.get_bytes_as('fulltext')
                        # We don't care about search_key_func for this code,
                        # because we only care about external references.
                        node = chk_map._deserialise(bytes, record.key,
                                                    search_key_func=None)
                        common_base = node._search_prefix
                        if isinstance(node, chk_map.InternalNode):
                            handle_internal_node(node)
                        elif parse_leaf_nodes:
                            handle_leaf_node(node)
                        counter[0] += 1
                        if pb is not None:
                            pb.update('chk node', counter[0], total_keys)
                        yield record
                yield next_stream()
                # Double check that we won't be emitting any keys twice
                # If we get rid of the pre-calculation of all keys, we could
                # turn this around and do
                # next_keys.difference_update(seen_keys)
                # However, we also may have references to chk pages in another
                # pack file during autopack. We filter earlier, so we should no
                # longer need to do this
                # next_keys = next_keys.intersection(remaining_keys)
                cur_keys = []
                for prefix in sorted(keys_by_search_prefix):
                    cur_keys.extend(keys_by_search_prefix.pop(prefix))
        for stream in _get_referenced_stream(self._chk_id_roots,
                                             self._gather_text_refs):
            yield stream
        del self._chk_id_roots
        # while it isn't really possible for chk_id_roots to not be in the
        # local group of packs, it is possible that the tree shape has not
        # changed recently, so we need to filter _chk_p_id_roots by the
        # available keys
        chk_p_id_roots = [key for key in self._chk_p_id_roots
                          if key in remaining_keys]
        del self._chk_p_id_roots
        for stream in _get_referenced_stream(chk_p_id_roots, False):
            yield stream
        if remaining_keys:
            trace.mutter('There were %d keys in the chk index, %d of which'
                         ' were not referenced', total_keys,
                         len(remaining_keys))
            if self.revision_ids is None:
                stream = source_vf.get_record_stream(remaining_keys,
                                                     'unordered', True)
                yield stream

    def _build_vf(self, index_name, parents, delta, for_write=False):
        """Build a VersionedFiles instance on top of this group of packs."""
        index_name = index_name + '_index'
        index_to_pack = {}
        access = knit._DirectPackAccess(index_to_pack)
        if for_write:
            # Use new_pack
            if self.new_pack is None:
                raise AssertionError('No new pack has been set')
            index = getattr(self.new_pack, index_name)
            index_to_pack[index] = self.new_pack.access_tuple()
            index.set_optimize(for_size=True)
            access.set_writer(self.new_pack._writer, index,
                              self.new_pack.access_tuple())
            add_callback = index.add_nodes
        else:
            indices = []
            for pack in self.packs:
                sub_index = getattr(pack, index_name)
                index_to_pack[sub_index] = pack.access_tuple()
                indices.append(sub_index)
            index = _mod_index.CombinedGraphIndex(indices)
            add_callback = None
        vf = GroupCompressVersionedFiles(
            _GCGraphIndex(index,
                          add_callback=add_callback,
                          parents=parents,
                          is_locked=self._pack_collection.repo.is_locked),
            access=access,
            delta=delta)
        return vf

    def _build_vfs(self, index_name, parents, delta):
        """Build the source and target VersionedFiles."""
        source_vf = self._build_vf(index_name, parents,
                                   delta, for_write=False)
        target_vf = self._build_vf(index_name, parents,
                                   delta, for_write=True)
        return source_vf, target_vf

    def _copy_stream(self, source_vf, target_vf, keys, message, vf_to_stream,
                     pb_offset):
        trace.mutter('repacking %d %s', len(keys), message)
        self.pb.update('repacking %s' % (message,), pb_offset)
        child_pb = ui.ui_factory.nested_progress_bar()
        try:
            stream = vf_to_stream(source_vf, keys, message, child_pb)
            for _ in target_vf._insert_record_stream(stream,
                                                     random_id=True,
                                                     reuse_blocks=False):
                pass
        finally:
            child_pb.finished()

    def _copy_revision_texts(self):
        source_vf, target_vf = self._build_vfs('revision', True, False)
        if not self.revision_keys:
            # We are doing a full fetch, aka 'pack'
            self.revision_keys = source_vf.keys()
        self._copy_stream(source_vf, target_vf, self.revision_keys,
                          'revisions', self._get_progress_stream, 1)

    def _copy_inventory_texts(self):
        source_vf, target_vf = self._build_vfs('inventory', True, True)
        self._copy_stream(source_vf, target_vf, self.revision_keys,
                          'inventories', self._get_filtered_inv_stream, 2)

    def _copy_chk_texts(self):
        source_vf, target_vf = self._build_vfs('chk', False, False)
        # TODO: This is technically spurious... if it is a performance issue,
        #       remove it
        total_keys = source_vf.keys()
        trace.mutter('repacking chk: %d id_to_entry roots,'
                     ' %d p_id_map roots, %d total keys',
                     len(self._chk_id_roots), len(self._chk_p_id_roots),
                     len(total_keys))
        self.pb.update('repacking chk', 3)
        child_pb = ui.ui_factory.nested_progress_bar()
        try:
            for stream in self._get_chk_streams(source_vf, total_keys,
                                                pb=child_pb):
                for _ in target_vf._insert_record_stream(stream,
                                                         random_id=True,
                                                         reuse_blocks=False):
                    pass
        finally:
            child_pb.finished()

    def _copy_text_texts(self):
        source_vf, target_vf = self._build_vfs('text', True, True)
        # XXX: We don't walk the chk map to determine referenced (file_id,
        #      revision_id) keys.  We don't do it yet because you really need
        #      to filter out the ones that are present in the parents of the
        #      rev just before the ones you are copying, otherwise the filter
        #      is grabbing too many keys...
        text_keys = source_vf.keys()
        self._copy_stream(source_vf, target_vf, text_keys,
                          'text', self._get_progress_stream, 4)

    def _copy_signature_texts(self):
        source_vf, target_vf = self._build_vfs('signature', False, False)
        signature_keys = source_vf.keys()
        signature_keys.intersection(self.revision_keys)
        self._copy_stream(source_vf, target_vf, signature_keys,
                          'signatures', self._get_progress_stream, 5)

    def _create_pack_from_packs(self):
        self.pb.update('repacking', 0, 7)
        self.new_pack = self.open_pack()
        # Is this necessary for GC ?
        self.new_pack.set_write_cache_size(1024*1024)
        self._copy_revision_texts()
        self._copy_inventory_texts()
        self._copy_chk_texts()
        self._copy_text_texts()
        self._copy_signature_texts()
        self.new_pack._check_references()
        if not self._use_pack(self.new_pack):
            self.new_pack.abort()
            return None
        self.pb.update('finishing repack', 6, 7)
        self.new_pack.finish()
        self._pack_collection.allocate(self.new_pack)
        return self.new_pack


class GCCHKReconcilePacker(GCCHKPacker):
    """A packer which regenerates indices etc as it copies.

    This is used by ``bzr reconcile`` to cause parent text pointers to be
    regenerated.
    """

    def __init__(self, *args, **kwargs):
        super(GCCHKReconcilePacker, self).__init__(*args, **kwargs)
        self._data_changed = False
        self._gather_text_refs = True

    def _copy_inventory_texts(self):
        source_vf, target_vf = self._build_vfs('inventory', True, True)
        self._copy_stream(source_vf, target_vf, self.revision_keys,
                          'inventories', self._get_filtered_inv_stream, 2)
        if source_vf.keys() != self.revision_keys:
            self._data_changed = True

    def _copy_text_texts(self):
        """generate what texts we should have and then copy."""
        source_vf, target_vf = self._build_vfs('text', True, True)
        trace.mutter('repacking %d texts', len(self._text_refs))
        self.pb.update("repacking texts", 4)
        # we have three major tasks here:
        # 1) generate the ideal index
        repo = self._pack_collection.repo
        # We want the one we just wrote, so base it on self.new_pack
        revision_vf = self._build_vf('revision', True, False, for_write=True)
        ancestor_keys = revision_vf.get_parent_map(revision_vf.keys())
        # Strip keys back into revision_ids.
        ancestors = dict((k[0], tuple([p[0] for p in parents]))
                         for k, parents in ancestor_keys.iteritems())
        del ancestor_keys
        # TODO: _generate_text_key_index should be much cheaper to generate from
        #       a chk repository, rather than the current implementation
        ideal_index = repo._generate_text_key_index(None, ancestors)
        file_id_parent_map = source_vf.get_parent_map(self._text_refs)
        # 2) generate a keys list that contains all the entries that can
        #    be used as-is, with corrected parents.
        ok_keys = []
        new_parent_keys = {} # (key, parent_keys)
        discarded_keys = []
        NULL_REVISION = _mod_revision.NULL_REVISION
        for key in self._text_refs:
            # 0 - index
            # 1 - key
            # 2 - value
            # 3 - refs
            try:
                ideal_parents = tuple(ideal_index[key])
            except KeyError:
                discarded_keys.append(key)
                self._data_changed = True
            else:
                if ideal_parents == (NULL_REVISION,):
                    ideal_parents = ()
                source_parents = file_id_parent_map[key]
                if ideal_parents == source_parents:
                    # no change needed.
                    ok_keys.append(key)
                else:
                    # We need to change the parent graph, but we don't need to
                    # re-insert the text (since we don't pun the compression
                    # parent with the parents list)
                    self._data_changed = True
                    new_parent_keys[key] = ideal_parents
        # we're finished with some data.
        del ideal_index
        del file_id_parent_map
        # 3) bulk copy the data, updating records than need it
        def _update_parents_for_texts():
            stream = source_vf.get_record_stream(self._text_refs,
                'groupcompress', False)
            for record in stream:
                if record.key in new_parent_keys:
                    record.parents = new_parent_keys[record.key]
                yield record
        target_vf.insert_record_stream(_update_parents_for_texts())

    def _use_pack(self, new_pack):
        """Override _use_pack to check for reconcile having changed content."""
        return new_pack.data_inserted() and self._data_changed


class GCRepositoryPackCollection(RepositoryPackCollection):

    pack_factory = GCPack
    resumed_pack_factory = ResumedGCPack

    def _already_packed(self):
        """Is the collection already packed?"""
        # Always repack GC repositories for now
        return False

    def _execute_pack_operations(self, pack_operations,
                                 _packer_class=GCCHKPacker,
                                 reload_func=None):
        """Execute a series of pack operations.

        :param pack_operations: A list of [revision_count, packs_to_combine].
        :param _packer_class: The class of packer to use (default: Packer).
        :return: None.
        """
        # XXX: Copied across from RepositoryPackCollection simply because we
        #      want to override the _packer_class ... :(
        for revision_count, packs in pack_operations:
            # we may have no-ops from the setup logic
            if len(packs) == 0:
                continue
            packer = GCCHKPacker(self, packs, '.autopack',
                                 reload_func=reload_func)
            try:
                packer.pack()
            except errors.RetryWithNewPacks:
                # An exception is propagating out of this context, make sure
                # this packer has cleaned up. Packer() doesn't set its new_pack
                # state into the RepositoryPackCollection object, so we only
                # have access to it directly here.
                if packer.new_pack is not None:
                    packer.new_pack.abort()
                raise
            for pack in packs:
                self._remove_pack_from_memory(pack)
        # record the newly available packs and stop advertising the old
        # packs
        self._save_pack_names(clear_obsolete_packs=True)
        # Move the old packs out of the way now they are no longer referenced.
        for revision_count, packs in pack_operations:
            self._obsolete_packs(packs)


class CHKInventoryRepository(KnitPackRepository):
    """subclass of KnitPackRepository that uses CHK based inventories."""

    def __init__(self, _format, a_bzrdir, control_files, _commit_builder_class,
        _serializer):
        """Overridden to change pack collection class."""
        KnitPackRepository.__init__(self, _format, a_bzrdir, control_files,
            _commit_builder_class, _serializer)
        # and now replace everything it did :)
        index_transport = self._transport.clone('indices')
        self._pack_collection = GCRepositoryPackCollection(self,
            self._transport, index_transport,
            self._transport.clone('upload'),
            self._transport.clone('packs'),
            _format.index_builder_class,
            _format.index_class,
            use_chk_index=self._format.supports_chks,
            )
        self.inventories = GroupCompressVersionedFiles(
            _GCGraphIndex(self._pack_collection.inventory_index.combined_index,
                add_callback=self._pack_collection.inventory_index.add_callback,
                parents=True, is_locked=self.is_locked,
                inconsistency_fatal=False),
            access=self._pack_collection.inventory_index.data_access)
        self.revisions = GroupCompressVersionedFiles(
            _GCGraphIndex(self._pack_collection.revision_index.combined_index,
                add_callback=self._pack_collection.revision_index.add_callback,
                parents=True, is_locked=self.is_locked,
                track_external_parent_refs=True),
            access=self._pack_collection.revision_index.data_access,
            delta=False)
        self.signatures = GroupCompressVersionedFiles(
            _GCGraphIndex(self._pack_collection.signature_index.combined_index,
                add_callback=self._pack_collection.signature_index.add_callback,
                parents=False, is_locked=self.is_locked,
                inconsistency_fatal=False),
            access=self._pack_collection.signature_index.data_access,
            delta=False)
        self.texts = GroupCompressVersionedFiles(
            _GCGraphIndex(self._pack_collection.text_index.combined_index,
                add_callback=self._pack_collection.text_index.add_callback,
                parents=True, is_locked=self.is_locked,
                inconsistency_fatal=False),
            access=self._pack_collection.text_index.data_access)
        # No parents, individual CHK pages don't have specific ancestry
        self.chk_bytes = GroupCompressVersionedFiles(
            _GCGraphIndex(self._pack_collection.chk_index.combined_index,
                add_callback=self._pack_collection.chk_index.add_callback,
                parents=False, is_locked=self.is_locked,
                inconsistency_fatal=False),
            access=self._pack_collection.chk_index.data_access)
        # True when the repository object is 'write locked' (as opposed to the
        # physical lock only taken out around changes to the pack-names list.)
        # Another way to represent this would be a decorator around the control
        # files object that presents logical locks as physical ones - if this
        # gets ugly consider that alternative design. RBC 20071011
        self._write_lock_count = 0
        self._transaction = None
        # for tests
        self._reconcile_does_inventory_gc = True
        self._reconcile_fixes_text_parents = True
        self._reconcile_backsup_inventory = False

    def _add_inventory_checked(self, revision_id, inv, parents):
        """Add inv to the repository after checking the inputs.

        This function can be overridden to allow different inventory styles.

        :seealso: add_inventory, for the contract.
        """
        # make inventory
        serializer = self._format._serializer
        result = inventory.CHKInventory.from_inventory(self.chk_bytes, inv,
            maximum_size=serializer.maximum_size,
            search_key_name=serializer.search_key_name)
        inv_lines = result.to_lines()
        return self._inventory_add_lines(revision_id, parents,
            inv_lines, check_content=False)

    def _create_inv_from_null(self, delta, revision_id):
        """This will mutate new_inv directly.

        This is a simplified form of create_by_apply_delta which knows that all
        the old values must be None, so everything is a create.
        """
        serializer = self._format._serializer
        new_inv = inventory.CHKInventory(serializer.search_key_name)
        new_inv.revision_id = revision_id
        entry_to_bytes = new_inv._entry_to_bytes
        id_to_entry_dict = {}
        parent_id_basename_dict = {}
        for old_path, new_path, file_id, entry in delta:
            if old_path is not None:
                raise ValueError('Invalid delta, somebody tried to delete %r'
                                 ' from the NULL_REVISION'
                                 % ((old_path, file_id),))
            if new_path is None:
                raise ValueError('Invalid delta, delta from NULL_REVISION has'
                                 ' no new_path %r' % (file_id,))
            if new_path == '':
                new_inv.root_id = file_id
                parent_id_basename_key = ('', '')
            else:
                utf8_entry_name = entry.name.encode('utf-8')
                parent_id_basename_key = (entry.parent_id, utf8_entry_name)
            new_value = entry_to_bytes(entry)
            # Populate Caches?
            # new_inv._path_to_fileid_cache[new_path] = file_id
            id_to_entry_dict[(file_id,)] = new_value
            parent_id_basename_dict[parent_id_basename_key] = file_id

        new_inv._populate_from_dicts(self.chk_bytes, id_to_entry_dict,
            parent_id_basename_dict, maximum_size=serializer.maximum_size)
        return new_inv

    def add_inventory_by_delta(self, basis_revision_id, delta, new_revision_id,
                               parents, basis_inv=None, propagate_caches=False):
        """Add a new inventory expressed as a delta against another revision.

        :param basis_revision_id: The inventory id the delta was created
            against.
        :param delta: The inventory delta (see Inventory.apply_delta for
            details).
        :param new_revision_id: The revision id that the inventory is being
            added for.
        :param parents: The revision ids of the parents that revision_id is
            known to have and are in the repository already. These are supplied
            for repositories that depend on the inventory graph for revision
            graph access, as well as for those that pun ancestry with delta
            compression.
        :param basis_inv: The basis inventory if it is already known,
            otherwise None.
        :param propagate_caches: If True, the caches for this inventory are
          copied to and updated for the result if possible.

        :returns: (validator, new_inv)
            The validator(which is a sha1 digest, though what is sha'd is
            repository format specific) of the serialized inventory, and the
            resulting inventory.
        """
        if not self.is_in_write_group():
            raise AssertionError("%r not in write group" % (self,))
        _mod_revision.check_not_reserved_id(new_revision_id)
        basis_tree = None
        if basis_inv is None:
            if basis_revision_id == _mod_revision.NULL_REVISION:
                new_inv = self._create_inv_from_null(delta, new_revision_id)
                inv_lines = new_inv.to_lines()
                return self._inventory_add_lines(new_revision_id, parents,
                    inv_lines, check_content=False), new_inv
            else:
                basis_tree = self.revision_tree(basis_revision_id)
                basis_tree.lock_read()
                basis_inv = basis_tree.inventory
        try:
            result = basis_inv.create_by_apply_delta(delta, new_revision_id,
                propagate_caches=propagate_caches)
            inv_lines = result.to_lines()
            return self._inventory_add_lines(new_revision_id, parents,
                inv_lines, check_content=False), result
        finally:
            if basis_tree is not None:
                basis_tree.unlock()

    def _iter_inventories(self, revision_ids):
        """Iterate over many inventory objects."""
        keys = [(revision_id,) for revision_id in revision_ids]
        stream = self.inventories.get_record_stream(keys, 'unordered', True)
        texts = {}
        for record in stream:
            if record.storage_kind != 'absent':
                texts[record.key] = record.get_bytes_as('fulltext')
            else:
                raise errors.NoSuchRevision(self, record.key)
        for key in keys:
            yield inventory.CHKInventory.deserialise(self.chk_bytes, texts[key], key)

    def _iter_inventory_xmls(self, revision_ids):
        # Without a native 'xml' inventory, this method doesn't make sense, so
        # make it raise to trap naughty direct users.
        raise NotImplementedError(self._iter_inventory_xmls)

    def _find_present_inventory_keys(self, revision_keys):
        parent_map = self.inventories.get_parent_map(revision_keys)
        present_inventory_keys = set(k for k in parent_map)
        return present_inventory_keys

    def fileids_altered_by_revision_ids(self, revision_ids, _inv_weave=None):
        """Find the file ids and versions affected by revisions.

        :param revisions: an iterable containing revision ids.
        :param _inv_weave: The inventory weave from this repository or None.
            If None, the inventory weave will be opened automatically.
        :return: a dictionary mapping altered file-ids to an iterable of
            revision_ids. Each altered file-ids has the exact revision_ids that
            altered it listed explicitly.
        """
        rich_root = self.supports_rich_root()
        bytes_to_info = inventory.CHKInventory._bytes_to_utf8name_key
        file_id_revisions = {}
        pb = ui.ui_factory.nested_progress_bar()
        try:
            revision_keys = [(r,) for r in revision_ids]
            parent_keys = self._find_parent_keys_of_revisions(revision_keys)
            # TODO: instead of using _find_present_inventory_keys, change the
            #       code paths to allow missing inventories to be tolerated.
            #       However, we only want to tolerate missing parent
            #       inventories, not missing inventories for revision_ids
            present_parent_inv_keys = self._find_present_inventory_keys(
                                        parent_keys)
            present_parent_inv_ids = set(
                [k[-1] for k in present_parent_inv_keys])
            uninteresting_root_keys = set()
            interesting_root_keys = set()
            inventories_to_read = set(revision_ids)
            inventories_to_read.update(present_parent_inv_ids)
            for inv in self.iter_inventories(inventories_to_read):
                entry_chk_root_key = inv.id_to_entry.key()
                if inv.revision_id in present_parent_inv_ids:
                    uninteresting_root_keys.add(entry_chk_root_key)
                else:
                    interesting_root_keys.add(entry_chk_root_key)

            chk_bytes = self.chk_bytes
            for record, items in chk_map.iter_interesting_nodes(chk_bytes,
                        interesting_root_keys, uninteresting_root_keys,
                        pb=pb):
                for name, bytes in items:
                    (name_utf8, file_id, revision_id) = bytes_to_info(bytes)
                    if not rich_root and name_utf8 == '':
                        continue
                    try:
                        file_id_revisions[file_id].add(revision_id)
                    except KeyError:
                        file_id_revisions[file_id] = set([revision_id])
        finally:
            pb.finished()
        return file_id_revisions

    def find_text_key_references(self):
        """Find the text key references within the repository.

        :return: A dictionary mapping text keys ((fileid, revision_id) tuples)
            to whether they were referred to by the inventory of the
            revision_id that they contain. The inventory texts from all present
            revision ids are assessed to generate this report.
        """
        # XXX: Slow version but correct: rewrite as a series of delta
        # examinations/direct tree traversal. Note that that will require care
        # as a common node is reachable both from the inventory that added it,
        # and others afterwards.
        revision_keys = self.revisions.keys()
        result = {}
        rich_roots = self.supports_rich_root()
        pb = ui.ui_factory.nested_progress_bar()
        try:
            all_revs = self.all_revision_ids()
            total = len(all_revs)
            for pos, inv in enumerate(self.iter_inventories(all_revs)):
                pb.update("Finding text references", pos, total)
                for _, entry in inv.iter_entries():
                    if not rich_roots and entry.file_id == inv.root_id:
                        continue
                    key = (entry.file_id, entry.revision)
                    result.setdefault(key, False)
                    if entry.revision == inv.revision_id:
                        result[key] = True
            return result
        finally:
            pb.finished()

    def _reconcile_pack(self, collection, packs, extension, revs, pb):
        packer = GCCHKReconcilePacker(collection, packs, extension)
        return packer.pack(pb)

    def _get_source(self, to_format):
        """Return a source for streaming from this repository."""
        if isinstance(to_format, remote.RemoteRepositoryFormat):
            # Can't just check attributes on to_format with the current code,
            # work around this:
            to_format._ensure_real()
            to_format = to_format._custom_format
        if to_format.__class__ is self._format.__class__:
            # We must be exactly the same format, otherwise stuff like the chk
            # page layout might be different
            return GroupCHKStreamSource(self, to_format)
        return super(CHKInventoryRepository, self)._get_source(to_format)


class GroupCHKStreamSource(KnitPackStreamSource):
    """Used when both the source and target repo are GroupCHK repos."""

    def __init__(self, from_repository, to_format):
        """Create a StreamSource streaming from from_repository."""
        super(GroupCHKStreamSource, self).__init__(from_repository, to_format)
        self._revision_keys = None
        self._text_keys = None
        self._text_fetch_order = 'groupcompress'
        self._chk_id_roots = None
        self._chk_p_id_roots = None

    def _get_inventory_stream(self, inventory_keys, allow_absent=False):
        """Get a stream of inventory texts.

        When this function returns, self._chk_id_roots and self._chk_p_id_roots
        should be populated.
        """
        self._chk_id_roots = []
        self._chk_p_id_roots = []
        def _filtered_inv_stream():
            id_roots_set = set()
            p_id_roots_set = set()
            source_vf = self.from_repository.inventories
            stream = source_vf.get_record_stream(inventory_keys,
                                                 'groupcompress', True)
            for record in stream:
                if record.storage_kind == 'absent':
                    if allow_absent:
                        continue
                    else:
                        raise errors.NoSuchRevision(self, record.key)
                bytes = record.get_bytes_as('fulltext')
                chk_inv = inventory.CHKInventory.deserialise(None, bytes,
                                                             record.key)
                key = chk_inv.id_to_entry.key()
                if key not in id_roots_set:
                    self._chk_id_roots.append(key)
                    id_roots_set.add(key)
                p_id_map = chk_inv.parent_id_basename_to_file_id
                if p_id_map is None:
                    raise AssertionError('Parent id -> file_id map not set')
                key = p_id_map.key()
                if key not in p_id_roots_set:
                    p_id_roots_set.add(key)
                    self._chk_p_id_roots.append(key)
                yield record
            # We have finished processing all of the inventory records, we
            # don't need these sets anymore
            id_roots_set.clear()
            p_id_roots_set.clear()
        return ('inventories', _filtered_inv_stream())

    def _get_filtered_chk_streams(self, excluded_revision_keys):
        self._text_keys = set()
        excluded_revision_keys.discard(_mod_revision.NULL_REVISION)
        if not excluded_revision_keys:
            uninteresting_root_keys = set()
            uninteresting_pid_root_keys = set()
        else:
            # filter out any excluded revisions whose inventories are not
            # actually present
            # TODO: Update Repository.iter_inventories() to add
            #       ignore_missing=True
            present_keys = self.from_repository._find_present_inventory_keys(
                            excluded_revision_keys)
            present_ids = [k[-1] for k in present_keys]
            uninteresting_root_keys = set()
            uninteresting_pid_root_keys = set()
            for inv in self.from_repository.iter_inventories(present_ids):
                uninteresting_root_keys.add(inv.id_to_entry.key())
                uninteresting_pid_root_keys.add(
                    inv.parent_id_basename_to_file_id.key())
        bytes_to_info = inventory.CHKInventory._bytes_to_utf8name_key
        chk_bytes = self.from_repository.chk_bytes
        def _filter_id_to_entry():
            for record, items in chk_map.iter_interesting_nodes(chk_bytes,
                        self._chk_id_roots, uninteresting_root_keys):
                for name, bytes in items:
                    # Note: we don't care about name_utf8, because we are always
                    # rich-root = True
                    _, file_id, revision_id = bytes_to_info(bytes)
                    self._text_keys.add((file_id, revision_id))
                if record is not None:
                    yield record
            # Consumed
            self._chk_id_roots = None
        yield 'chk_bytes', _filter_id_to_entry()
        def _get_parent_id_basename_to_file_id_pages():
            for record, items in chk_map.iter_interesting_nodes(chk_bytes,
                        self._chk_p_id_roots, uninteresting_pid_root_keys):
                if record is not None:
                    yield record
            # Consumed
            self._chk_p_id_roots = None
        yield 'chk_bytes', _get_parent_id_basename_to_file_id_pages()

    def get_stream(self, search):
        revision_ids = search.get_keys()
        for stream_info in self._fetch_revision_texts(revision_ids):
            yield stream_info
        self._revision_keys = [(rev_id,) for rev_id in revision_ids]
        yield self._get_inventory_stream(self._revision_keys)
        # TODO: The keys to exclude might be part of the search recipe
        # For now, exclude all parents that are at the edge of ancestry, for
        # which we have inventories
        from_repo = self.from_repository
        parent_keys = from_repo._find_parent_keys_of_revisions(
                        self._revision_keys)
        for stream_info in self._get_filtered_chk_streams(parent_keys):
            yield stream_info
        yield self._get_text_stream()

    def get_stream_for_missing_keys(self, missing_keys):
        # missing keys can only occur when we are byte copying and not
        # translating (because translation means we don't send
        # unreconstructable deltas ever).
        missing_inventory_keys = set()
        for key in missing_keys:
            if key[0] != 'inventories':
                raise AssertionError('The only missing keys we should'
                    ' be filling in are inventory keys, not %s'
                    % (key[0],))
            missing_inventory_keys.add(key[1:])
        if self._chk_id_roots or self._chk_p_id_roots:
            raise AssertionError('Cannot call get_stream_for_missing_keys'
                ' untill all of get_stream() has been consumed.')
        # Yield the inventory stream, so we can find the chk stream
        # Some of the missing_keys will be missing because they are ghosts.
        # As such, we can ignore them. The Sink is required to verify there are
        # no unavailable texts when the ghost inventories are not filled in.
        yield self._get_inventory_stream(missing_inventory_keys,
                                         allow_absent=True)
        # We use the empty set for excluded_revision_keys, to make it clear
        # that we want to transmit all referenced chk pages.
        for stream_info in self._get_filtered_chk_streams(set()):
            yield stream_info


class RepositoryFormatCHK1(RepositoryFormatPack):
    """A hashed CHK+group compress pack repository."""

    repository_class = CHKInventoryRepository
    supports_external_lookups = True
    supports_chks = True
    # For right now, setting this to True gives us InterModel1And2 rather
    # than InterDifferingSerializer
    _commit_builder_class = PackRootCommitBuilder
    rich_root_data = True
    _serializer = chk_serializer.chk_serializer_255_bigpage
    _commit_inv_deltas = True
    # What index classes to use
    index_builder_class = BTreeBuilder
    index_class = BTreeGraphIndex
    # Note: We cannot unpack a delta that references a text we haven't
    # seen yet. There are 2 options, work in fulltexts, or require
    # topological sorting. Using fulltexts is more optimal for local
    # operations, because the source can be smart about extracting
    # multiple in-a-row (and sharing strings). Topological is better
    # for remote, because we access less data.
    _fetch_order = 'unordered'
    _fetch_uses_deltas = False # essentially ignored by the groupcompress code.
    fast_deltas = True

    def _get_matching_bzrdir(self):
        return bzrdir.format_registry.make_bzrdir('development6-rich-root')

    def _ignore_setting_bzrdir(self, format):
        pass

    _matchingbzrdir = property(_get_matching_bzrdir, _ignore_setting_bzrdir)

    def get_format_string(self):
        """See RepositoryFormat.get_format_string()."""
        return ('Bazaar development format - group compression and chk inventory'
                ' (needs bzr.dev from 1.14)\n')

    def get_format_description(self):
        """See RepositoryFormat.get_format_description()."""
        return ("Development repository format - rich roots, group compression"
            " and chk inventories")

    def check_conversion_target(self, target_format):
        if not target_format.rich_root_data:
            raise errors.BadConversionTarget(
                'Does not support rich root data.', target_format)
        if not getattr(target_format, 'supports_tree_reference', False):
            raise errors.BadConversionTarget(
                'Does not support nested trees', target_format)



class RepositoryFormatCHK2(RepositoryFormatCHK1):
    """A CHK repository that uses the bencode revision serializer."""

    _serializer = chk_serializer.chk_bencode_serializer

    def _get_matching_bzrdir(self):
        return bzrdir.format_registry.make_bzrdir('development7-rich-root')

    def _ignore_setting_bzrdir(self, format):
        pass

    _matchingbzrdir = property(_get_matching_bzrdir, _ignore_setting_bzrdir)

    def get_format_string(self):
        """See RepositoryFormat.get_format_string()."""
        return ('Bazaar development format - chk repository with bencode '
                'revision serialization (needs bzr.dev from 1.16)\n')


class RepositoryFormat2a(RepositoryFormatCHK2):
    """A CHK repository that uses the bencode revision serializer.
    
    This is the same as RepositoryFormatCHK2 but with a public name.
    """

    _serializer = chk_serializer.chk_bencode_serializer

    def _get_matching_bzrdir(self):
        return bzrdir.format_registry.make_bzrdir('2a')

    def _ignore_setting_bzrdir(self, format):
        pass

    _matchingbzrdir = property(_get_matching_bzrdir, _ignore_setting_bzrdir)

    def get_format_string(self):
        return ('Bazaar repository format 2a (needs bzr 1.16 or later)\n')
