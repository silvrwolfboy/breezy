# Copyright (C) 2006-2010 Canonical Ltd
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

"""Tests for branch.pull behaviour."""

from bzrlib import (
    branch,
    bzrdir,
    errors,
    memorytree,
    revision,
    )
from bzrlib.tests import (
    fixtures,
    per_branch,
    TestNotApplicable,
    )


class TestPull(per_branch.TestCaseWithBranch):

    def test_pull_convergence_simple(self):
        # when revisions are pulled, the left-most accessible parents must
        # become the revision-history.
        parent = self.make_branch_and_tree('parent')
        parent.commit('1st post', rev_id='P1', allow_pointless=True)
        mine = parent.bzrdir.sprout('mine').open_workingtree()
        mine.commit('my change', rev_id='M1', allow_pointless=True)
        parent.merge_from_branch(mine.branch)
        parent.commit('merge my change', rev_id='P2')
        mine.pull(parent.branch)
        self.assertEqual('P2', mine.branch.last_revision())

    def test_pull_merged_indirect(self):
        # it should be possible to do a pull from one branch into another
        # when the tip of the target was merged into the source branch
        # via a third branch - so its buried in the ancestry and is not
        # directly accessible.
        parent = self.make_branch_and_tree('parent')
        parent.commit('1st post', rev_id='P1', allow_pointless=True)
        mine = parent.bzrdir.sprout('mine').open_workingtree()
        mine.commit('my change', rev_id='M1', allow_pointless=True)
        other = parent.bzrdir.sprout('other').open_workingtree()
        other.merge_from_branch(mine.branch)
        other.commit('merge my change', rev_id='O2')
        parent.merge_from_branch(other.branch)
        parent.commit('merge other', rev_id='P2')
        mine.pull(parent.branch)
        self.assertEqual('P2', mine.branch.last_revision())

    def test_pull_updates_checkout_and_master(self):
        """Pulling into a checkout updates the checkout and the master branch"""
        master_tree = self.make_branch_and_tree('master')
        rev1 = master_tree.commit('master')
        checkout = master_tree.branch.create_checkout('checkout')

        other = master_tree.branch.bzrdir.sprout('other').open_workingtree()
        rev2 = other.commit('other commit')
        # now pull, which should update both checkout and master.
        checkout.branch.pull(other.branch)
        self.assertEqual(rev2, checkout.branch.last_revision())
        self.assertEqual(rev2, master_tree.branch.last_revision())

    def test_pull_local_updates_checkout_only(self):
        """Pulling --local into a checkout updates the checkout and not the
        master branch"""
        master_tree = self.make_branch_and_tree('master')
        rev1 = master_tree.commit('master')
        checkout = master_tree.branch.create_checkout('checkout')

        other = master_tree.branch.bzrdir.sprout('other').open_workingtree()
        rev2 = other.commit('other commit')
        # now pull local, which should update checkout but not master.
        checkout.branch.pull(other.branch, local = True)
        self.assertEqual(rev2, checkout.branch.last_revision())
        self.assertEqual(rev1, master_tree.branch.last_revision())

    def test_pull_local_raises_LocalRequiresBoundBranch_on_unbound(self):
        """Pulling --local into a branch that is not bound should fail."""
        master_tree = self.make_branch_and_tree('branch')
        rev1 = master_tree.commit('master')

        other = master_tree.branch.bzrdir.sprout('other').open_workingtree()
        rev2 = other.commit('other commit')
        # now pull --local, which should raise LocalRequiresBoundBranch error.
        self.assertRaises(errors.LocalRequiresBoundBranch,
                          master_tree.branch.pull, other.branch, local = True)
        self.assertEqual(rev1, master_tree.branch.last_revision())

    def test_pull_returns_result(self):
        parent = self.make_branch_and_tree('parent')
        parent.commit('1st post', rev_id='P1')
        mine = parent.bzrdir.sprout('mine').open_workingtree()
        mine.commit('my change', rev_id='M1')
        result = parent.branch.pull(mine.branch)
        self.assertIsNot(None, result)
        self.assertIs(mine.branch, result.source_branch)
        self.assertIs(parent.branch, result.target_branch)
        self.assertIs(parent.branch, result.master_branch)
        self.assertIs(None, result.local_branch)
        self.assertEqual(1, result.old_revno)
        self.assertEqual('P1', result.old_revid)
        self.assertEqual(2, result.new_revno)
        self.assertEqual('M1', result.new_revid)
        self.assertEqual([], result.tag_conflicts)

    def test_pull_overwrite(self):
        tree_a = self.make_branch_and_tree('tree_a')
        tree_a.commit('message 1')
        tree_b = tree_a.bzrdir.sprout('tree_b').open_workingtree()
        tree_a.commit('message 2', rev_id='rev2a')
        tree_b.commit('message 2', rev_id='rev2b')
        self.assertRaises(errors.DivergedBranches, tree_a.pull, tree_b.branch)
        self.assertRaises(errors.DivergedBranches,
                          tree_a.branch.pull, tree_b.branch,
                          overwrite=False, stop_revision='rev2b')
        # It should not have updated the branch tip, but it should have fetched
        # the revision if the repository supports "invisible" revisions
        self.assertEqual('rev2a', tree_a.branch.last_revision())
        if tree_a.branch.repository._format.supports_unreferenced_revisions:
            self.assertTrue(tree_a.branch.repository.has_revision('rev2b'))
        tree_a.branch.pull(tree_b.branch, overwrite=True,
                           stop_revision='rev2b')
        self.assertEqual('rev2b', tree_a.branch.last_revision())
        self.assertEqual(tree_b.branch.last_revision(),
                         tree_a.branch.last_revision())

    def test_pull_merges_and_fetches_tags(self):
        """Tags are updated by br.pull(source), and revisions named in those
        tags are fetched.
        """
        # Make a source, sprout a target off it
        try:
            builder = self.make_branch_builder('source')
        except errors.UninitializableFormat:
            raise TestNotApplicable('uninitializeable format')
        source = fixtures.build_branch_with_non_ancestral_rev(builder)
        target = source.bzrdir.sprout('target').open_branch()
        # Add a tag to the source, then pull from source
        try:
            source.tags.set_tag('tag-a', 'rev-2')
        except errors.TagsNotSupported:
            raise TestNotApplicable('format does not support tags.')
        source.tags.set_tag('tag-a', 'rev-2')
        source.get_config().set_user_option('branch.fetch_tags', 'True')
        target.pull(source)
        # The tag is present, and so is its revision.
        self.assertEqual('rev-2', target.tags.lookup_tag('tag-a'))
        target.repository.get_revision('rev-2')

    def test_pull_stop_revision_merges_and_fetches_tags(self):
        """br.pull(source, stop_revision=REV) updates and fetches tags."""
        # Make a source, sprout a target off it
        try:
            builder = self.make_branch_builder('source')
        except errors.UninitializableFormat:
            raise TestNotApplicable('uninitializeable format')
        source = fixtures.build_branch_with_non_ancestral_rev(builder)
        target = source.bzrdir.sprout('target').open_branch()
        # Add a new commit to the ancestry
        builder.build_commit(message="Rev 2 again", rev_id='rev-2-again')
        # Add a tag to the source, then pull rev-2-again from source
        try:
            source.tags.set_tag('tag-a', 'rev-2')
        except errors.TagsNotSupported:
            raise TestNotApplicable('format does not support tags.')
        source.get_config().set_user_option('branch.fetch_tags', 'True')
        target.pull(source, 'rev-2-again')
        # The tag is present, and so is its revision.
        self.assertEqual('rev-2', target.tags.lookup_tag('tag-a'))
        target.repository.get_revision('rev-2')


class TestPullHook(per_branch.TestCaseWithBranch):

    def setUp(self):
        self.hook_calls = []
        super(TestPullHook, self).setUp()

    def capture_post_pull_hook(self, result):
        """Capture post pull hook calls to self.hook_calls.

        The call is logged, as is some state of the two branches.
        """
        if result.local_branch:
            local_locked = result.local_branch.is_locked()
            local_base = result.local_branch.base
        else:
            local_locked = None
            local_base = None
        self.hook_calls.append(
            ('post_pull', result.source_branch, local_base,
             result.master_branch.base, result.old_revno,
             result.old_revid,
             result.new_revno, result.new_revid,
             result.source_branch.is_locked(), local_locked,
             result.master_branch.is_locked()))

    def test_post_pull_empty_history(self):
        target = self.make_branch('target')
        source = self.make_branch('source')
        branch.Branch.hooks.install_named_hook(
            'post_pull', self.capture_post_pull_hook, None)
        target.pull(source)
        # with nothing there we should still get a notification, and
        # have both branches locked at the notification time.
        self.assertEqual([
            ('post_pull', source, None, target.base, 0, revision.NULL_REVISION,
             0, revision.NULL_REVISION, True, None, True)
            ],
            self.hook_calls)

    def test_post_pull_bound_branch(self):
        # pulling to a bound branch should pass in the master branch to the
        # hook, allowing the correct number of emails to be sent, while still
        # allowing hooks that want to modify the target to do so to both
        # instances.
        target = self.make_branch('target')
        local = self.make_branch('local')
        try:
            local.bind(target)
        except errors.UpgradeRequired:
            # We can't bind this format to itself- typically it is the local
            # branch that doesn't support binding.  As of May 2007
            # remotebranches can't be bound.  Let's instead make a new local
            # branch of the default type, which does allow binding.
            # See https://bugs.launchpad.net/bzr/+bug/112020
            local = bzrdir.BzrDir.create_branch_convenience('local2')
            local.bind(target)
        source = self.make_branch('source')
        branch.Branch.hooks.install_named_hook(
            'post_pull', self.capture_post_pull_hook, None)
        local.pull(source)
        # with nothing there we should still get a notification, and
        # have both branches locked at the notification time.
        self.assertEqual([
            ('post_pull', source, local.base, target.base, 0,
             revision.NULL_REVISION, 0, revision.NULL_REVISION,
             True, True, True)
            ],
            self.hook_calls)

    def test_post_pull_nonempty_history(self):
        target = self.make_branch_and_memory_tree('target')
        target.lock_write()
        target.add('')
        rev1 = target.commit('rev 1')
        target.unlock()
        sourcedir = target.bzrdir.clone(self.get_url('source'))
        source = memorytree.MemoryTree.create_on_branch(sourcedir.open_branch())
        rev2 = source.commit('rev 2')
        branch.Branch.hooks.install_named_hook(
            'post_pull', self.capture_post_pull_hook, None)
        target.branch.pull(source.branch)
        # with nothing there we should still get a notification, and
        # have both branches locked at the notification time.
        self.assertEqual([
            ('post_pull', source.branch, None, target.branch.base, 1, rev1,
             2, rev2, True, None, True)
            ],
            self.hook_calls)
