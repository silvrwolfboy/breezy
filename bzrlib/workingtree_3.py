# Copyright (C) 2007-2011 Canonical Ltd
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

"""WorkingTree3 format and implementation.

"""

from bzrlib import (
    bzrdir,
    errors,
    inventory,
    revision as _mod_revision,
    transform,
    )
from bzrlib.decorators import (
    needs_read_lock,
    )
from bzrlib.lockable_files import LockableFiles
from bzrlib.lockdir import LockDir
from bzrlib.transport.local import LocalTransport
from bzrlib.workingtree import (
    InventoryWorkingTree,
    WorkingTreeFormat,
    )

class WorkingTree3(InventoryWorkingTree):
    """This is the Format 3 working tree.

    This differs from the base WorkingTree by:
     - having its own file lock
     - having its own last-revision property.

    This is new in bzr 0.8
    """

    @needs_read_lock
    def _last_revision(self):
        """See Mutable.last_revision."""
        try:
            return self._transport.get_bytes('last-revision')
        except errors.NoSuchFile:
            return _mod_revision.NULL_REVISION

    def _change_last_revision(self, revision_id):
        """See WorkingTree._change_last_revision."""
        if revision_id is None or revision_id == _mod_revision.NULL_REVISION:
            try:
                self._transport.delete('last-revision')
            except errors.NoSuchFile:
                pass
            return False
        else:
            self._transport.put_bytes('last-revision', revision_id,
                mode=self.bzrdir._get_file_mode())
            return True

    def _get_check_refs(self):
        """Return the references needed to perform a check of this tree."""
        return [('trees', self.last_revision())]

    def unlock(self):
        # do non-implementation specific cleanup
        self._cleanup()
        if self._control_files._lock_count == 1:
            # _inventory_is_modified is always False during a read lock.
            if self._inventory_is_modified:
                self.flush()
            self._write_hashcache_if_dirty()
        # reverse order of locking.
        try:
            return self._control_files.unlock()
        finally:
            self.branch.unlock()


class WorkingTreeFormat3(WorkingTreeFormat):
    """The second working tree format updated to record a format marker.

    This format:
        - exists within a metadir controlling .bzr
        - includes an explicit version marker for the workingtree control
          files, separate from the BzrDir format
        - modifies the hash cache format
        - is new in bzr 0.8
        - uses a LockDir to guard access for writes.
    """

    upgrade_recommended = True

    missing_parent_conflicts = True

    def get_format_string(self):
        """See WorkingTreeFormat.get_format_string()."""
        return "Bazaar-NG Working Tree format 3"

    def get_format_description(self):
        """See WorkingTreeFormat.get_format_description()."""
        return "Working tree format 3"

    _tree_class = WorkingTree3

    def __get_matchingbzrdir(self):
        return bzrdir.BzrDirMetaFormat1()

    _matchingbzrdir = property(__get_matchingbzrdir)

    def _open_control_files(self, a_bzrdir):
        transport = a_bzrdir.get_workingtree_transport(None)
        return LockableFiles(transport, 'lock', LockDir)

    def initialize(self, a_bzrdir, revision_id=None, from_branch=None,
                   accelerator_tree=None, hardlink=False):
        """See WorkingTreeFormat.initialize().

        :param revision_id: if supplied, create a working tree at a different
            revision than the branch is at.
        :param accelerator_tree: A tree which can be used for retrieving file
            contents more quickly than the revision tree, i.e. a workingtree.
            The revision tree will be used for cases where accelerator_tree's
            content is different.
        :param hardlink: If true, hard-link files from accelerator_tree,
            where possible.
        """
        if not isinstance(a_bzrdir.transport, LocalTransport):
            raise errors.NotLocalUrl(a_bzrdir.transport.base)
        transport = a_bzrdir.get_workingtree_transport(self)
        control_files = self._open_control_files(a_bzrdir)
        control_files.create_lock()
        control_files.lock_write()
        transport.put_bytes('format', self.get_format_string(),
            mode=a_bzrdir._get_file_mode())
        if from_branch is not None:
            branch = from_branch
        else:
            branch = a_bzrdir.open_branch()
        if revision_id is None:
            revision_id = _mod_revision.ensure_null(branch.last_revision())
        # WorkingTree3 can handle an inventory which has a unique root id.
        # as of bzr 0.12. However, bzr 0.11 and earlier fail to handle
        # those trees. And because there isn't a format bump inbetween, we
        # are maintaining compatibility with older clients.
        # inv = Inventory(root_id=gen_root_id())
        inv = self._initial_inventory()
        wt = self._tree_class(a_bzrdir.root_transport.local_abspath('.'),
                         branch,
                         inv,
                         _internal=True,
                         _format=self,
                         _bzrdir=a_bzrdir,
                         _control_files=control_files)
        wt.lock_tree_write()
        try:
            basis_tree = branch.repository.revision_tree(revision_id)
            # only set an explicit root id if there is one to set.
            if basis_tree.inventory.root is not None:
                wt.set_root_id(basis_tree.get_root_id())
            if revision_id == _mod_revision.NULL_REVISION:
                wt.set_parent_trees([])
            else:
                wt.set_parent_trees([(revision_id, basis_tree)])
            transform.build_tree(basis_tree, wt)
        finally:
            # Unlock in this order so that the unlock-triggers-flush in
            # WorkingTree is given a chance to fire.
            control_files.unlock()
            wt.unlock()
        return wt

    def _initial_inventory(self):
        return inventory.Inventory()

    def open(self, a_bzrdir, _found=False):
        """Return the WorkingTree object for a_bzrdir

        _found is a private parameter, do not use it. It is used to indicate
               if format probing has already been done.
        """
        if not _found:
            # we are being called directly and must probe.
            raise NotImplementedError
        if not isinstance(a_bzrdir.transport, LocalTransport):
            raise errors.NotLocalUrl(a_bzrdir.transport.base)
        wt = self._open(a_bzrdir, self._open_control_files(a_bzrdir))
        return wt

    def _open(self, a_bzrdir, control_files):
        """Open the tree itself.

        :param a_bzrdir: the dir for the tree.
        :param control_files: the control files for the tree.
        """
        return self._tree_class(a_bzrdir.root_transport.local_abspath('.'),
                                _internal=True,
                                _format=self,
                                _bzrdir=a_bzrdir,
                                _control_files=control_files)

    def __str__(self):
        return self.get_format_string()