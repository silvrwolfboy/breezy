# Copyright (C) 2006 Canonical Ltd
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

"""Remove the last revision from the history of the current branch."""

# TODO: make the guts of this methods on tree, branch.

from bzrlib import (
    errors,
    revision as _mod_revision,
    )
from bzrlib.branch import Branch
from bzrlib.errors import BoundBranchOutOfDate


def uncommit(branch, dry_run=False, verbose=False, revno=None, tree=None,
             local=False):
    """Remove the last revision from the supplied branch.

    :param dry_run: Don't actually change anything
    :param verbose: Print each step as you do it
    :param revno: Remove back to this revision
    :param local: If this branch is bound, only remove the revisions from the
        local branch. If this branch is not bound, it is an error to pass
        local=True.
    """
    unlockable = []
    try:
        if tree is not None:
            tree.lock_write()
            unlockable.append(tree)

        branch.lock_write()
        unlockable.append(branch)

        pending_merges = []
        if tree is not None:
            pending_merges = tree.get_parent_ids()[1:]

        if local:
            master = None
            if branch.get_bound_location() is None:
                raise errors.LocalRequiresBoundBranch()
        else:
            master = branch.get_master_branch()
            if master is not None:
                master.lock_write()
                unlockable.append(master)
        old_revno, old_tip = branch.last_revision_info()
        if master is not None and old_tip != master.last_revision():
            raise BoundBranchOutOfDate(branch, master)
        if revno is None:
            revno = old_revno
        new_revno = revno - 1

        revid_iterator = branch.repository.iter_reverse_revision_history(
                            old_tip)
        cur_revno = old_revno
        new_revision_id = old_tip
        graph = branch.repository.get_graph()
        for rev_id in revid_iterator:
            if cur_revno == new_revno:
                new_revision_id = rev_id
                break
            if verbose:
                print 'Removing revno %d: %s' % (cur_revno, rev_id)
            cur_revno -= 1
            parents = graph.get_parent_map([rev_id]).get(rev_id, None)
            if not parents:
                continue
            # When we finish popping off the pending merges, we want
            # them to stay in the order that they used to be.
            # but we pop from the end, so reverse the order, and
            # then get the order right at the end
            pending_merges.extend(reversed(parents[1:]))
        else:
            # We ran off the end of revisions, which means we should be trying
            # to get to NULL_REVISION
            new_revision_id = _mod_revision.NULL_REVISION

        if not dry_run:
            if master is not None:
                master.set_last_revision_info(new_revno, new_revision_id)
            branch.set_last_revision_info(new_revno, new_revision_id)
            if master is None:
                hook_local = None
                hook_master = branch
            else:
                hook_local = branch
                hook_master = master
            for hook in Branch.hooks['post_uncommit']:
                hook_new_tip = new_revision_id
                if hook_new_tip == _mod_revision.NULL_REVISION:
                    hook_new_tip = None
                hook(hook_local, hook_master, old_revno, old_tip, new_revno,
                     hook_new_tip)
            if tree is not None:
                if not _mod_revision.is_null(new_revision_id):
                    parents = [new_revision_id]
                else:
                    parents = []
                parents.extend(reversed(pending_merges))
                tree.set_parent_ids(parents)
    finally:
        for item in reversed(unlockable):
            item.unlock()