# Copyright (C) 2007 Canonical Ltd
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
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""Tests for indices."""

from bzrlib import errors
from bzrlib.index import GraphIndexBuilder, GraphIndex
from bzrlib.tests import TestCaseWithMemoryTransport


class TestGraphIndexBuilder(TestCaseWithMemoryTransport):

    def test_build_index_empty(self):
        builder = GraphIndexBuilder()
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=0\n\n", contents)

    def test_build_index_one_reference_list_empty(self):
        builder = GraphIndexBuilder(reference_lists=1)
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=1\n\n", contents)

    def test_build_index_two_reference_list_empty(self):
        builder = GraphIndexBuilder(reference_lists=2)
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=2\n\n", contents)

    def test_build_index_one_node(self):
        builder = GraphIndexBuilder()
        builder.add_node('akey', (), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=0\n"
            "akey\0\0\0data\n\n", contents)

    def test_add_node_empty_value(self):
        builder = GraphIndexBuilder()
        builder.add_node('akey', (), '')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=0\n"
            "akey\0\0\0\n\n", contents)

    def test_build_index_two_nodes_sorted_reverse(self):
        # the highest sorted node comes first.
        builder = GraphIndexBuilder()
        # use three to have a good chance of glitching dictionary hash
        # lookups etc. Insert in randomish order that is not correct
        # and not the reverse of the correct order.
        builder.add_node('2001', (), 'data')
        builder.add_node('2000', (), 'data')
        builder.add_node('2002', (), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=0\n"
            "2002\0\0\0data\n"
            "2001\0\0\0data\n"
            "2000\0\0\0data\n"
            "\n", contents)

    def test_build_index_reference_lists_are_included_one(self):
        builder = GraphIndexBuilder(reference_lists=1)
        builder.add_node('key', ([], ), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=1\n"
            "key\0\0\0data\n"
            "\n", contents)

    def test_build_index_reference_lists_are_included_two(self):
        builder = GraphIndexBuilder(reference_lists=2)
        builder.add_node('key', ([], []), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=2\n"
            "key\0\0\t\0data\n"
            "\n", contents)

    def test_node_references_are_byte_offsets(self):
        builder = GraphIndexBuilder(reference_lists=1)
        builder.add_node('reference', ([], ), 'data')
        builder.add_node('key', (['reference'], ), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=1\n"
            "reference\0\0\0data\n"
            "key\0\x0038\0data\n"
            "\n", contents)

    def test_node_references_are_cr_delimited(self):
        builder = GraphIndexBuilder(reference_lists=1)
        builder.add_node('reference', ([], ), 'data')
        builder.add_node('reference2', ([], ), 'data')
        builder.add_node('key', (['reference', 'reference2'], ), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=1\n"
            "reference2\0\0\0data\n"
            "reference\0\0\0data\n"
            "key\0\x0056\r38\0data\n"
            "\n", contents)

    def test_multiple_reference_lists_are_tab_delimited(self):
        builder = GraphIndexBuilder(reference_lists=2)
        builder.add_node('reference', ([], []), 'data')
        builder.add_node('key', (['reference'], ['reference']), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=2\n"
            "reference\0\0\t\0data\n"
            "key\0\x0038\t38\0data\n"
            "\n", contents)

    def test_add_node_referencing_missing_key_makes_absent(self):
        builder = GraphIndexBuilder(reference_lists=1)
        builder.add_node('key', (['reference', 'reference2'], ), 'data')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=1\n"
            "reference2\0a\0\0\n"
            "reference\0a\0\0\n"
            "key\0\x0053\r38\0data\n"
            "\n", contents)

    def test_node_references_three_digits(self):
        # test the node digit expands as needed.
        builder = GraphIndexBuilder(reference_lists=1)
        references = map(str, range(9))
        builder.add_node('5-key', (references, ), '')
        stream = builder.finish()
        contents = stream.read()
        self.assertEqual("Bazaar Graph Index 1\nnode_ref_lists=1\n"
            "8\x00a\x00\x00\n"
            "7\x00a\x00\x00\n"
            "6\x00a\x00\x00\n"
            "5-key\x00\x00130\r124\r118\r112\r106\r100\r050\r044\r038\x00\n"
            "5\x00a\x00\x00\n"
            "4\x00a\x00\x00\n"
            "3\x00a\x00\x00\n"
            "2\x00a\x00\x00\n"
            "1\x00a\x00\x00\n"
            "0\x00a\x00\x00\n"
            "\n", contents)

    def test_add_node_bad_key(self):
        builder = GraphIndexBuilder()
        for bad_char in '\t\n\x0b\x0c\r\x00 ':
            self.assertRaises(errors.BadIndexKey, builder.add_node,
                'a%skey' % bad_char, (), 'data')
        self.assertRaises(errors.BadIndexKey, builder.add_node,
                '', (), 'data')

    def test_add_node_bad_data(self):
        builder = GraphIndexBuilder()
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            (), 'data\naa')
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            (), 'data\0aa')

    def test_add_node_bad_mismatched_ref_lists_length(self):
        builder = GraphIndexBuilder()
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            ([], ), 'data aa')
        builder = GraphIndexBuilder(reference_lists=1)
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            (), 'data aa')
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            ([], []), 'data aa')
        builder = GraphIndexBuilder(reference_lists=2)
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            (), 'data aa')
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            ([], ), 'data aa')
        self.assertRaises(errors.BadIndexValue, builder.add_node, 'akey',
            ([], [], []), 'data aa')

    def test_add_node_bad_key_in_reference_lists(self):
        # first list, first key - trivial
        builder = GraphIndexBuilder(reference_lists=1)
        self.assertRaises(errors.BadIndexKey, builder.add_node, 'akey',
            (['a key'], ), 'data aa')
        # need to check more than the first key in the list
        self.assertRaises(errors.BadIndexKey, builder.add_node, 'akey',
            (['agoodkey', 'this is a bad key'], ), 'data aa')
        # and if there is more than one list it should be getting checked
        # too
        builder = GraphIndexBuilder(reference_lists=2)
        self.assertRaises(errors.BadIndexKey, builder.add_node, 'akey',
            ([], ['a bad key']), 'data aa')

    def test_add_duplicate_key(self):
        builder = GraphIndexBuilder()
        builder.add_node('key', (), 'data')
        self.assertRaises(errors.BadIndexDuplicateKey, builder.add_node, 'key',
            (), 'data')

    def test_add_key_after_referencing_key(self):
        builder = GraphIndexBuilder(reference_lists=1)
        builder.add_node('key', (['reference'], ), 'data')
        builder.add_node('reference', ([],), 'data')


class TestGraphIndex(TestCaseWithMemoryTransport):

    def make_index(self, ref_lists=0, nodes=[]):
        builder = GraphIndexBuilder(ref_lists)
        for node, references, value in nodes:
            builder.add_node(node, references, value)
        stream = builder.finish()
        trans = self.get_transport()
        trans.put_file('index', stream)
        return GraphIndex(trans, 'index')

    def test_open_bad_index_no_error(self):
        trans = self.get_transport()
        trans.put_bytes('name', "not an index\n")
        index = GraphIndex(trans, 'name')

    def test_iter_all_entries_empty(self):
        index = self.make_index()
        self.assertEqual([], list(index.iter_all_entries()))

    def test_iter_all_entries_simple(self):
        index = self.make_index(nodes=[('name', (), 'data')])
        self.assertEqual([('name', (), 'data')],
            list(index.iter_all_entries()))

    def test_iter_nothing_empty(self):
        index = self.make_index()
        self.assertEqual([], list(index.iter_entries([])))

    def test_iter_missing_entry_empty(self):
        index = self.make_index()
        self.assertRaises(errors.MissingKey, list, index.iter_entries(['a']))

    def test_validate_bad_index_errors(self):
        trans = self.get_transport()
        trans.put_bytes('name', "not an index\n")
        index = GraphIndex(trans, 'name')
        self.assertRaises(errors.BadIndexFormatSignature, index.validate)

    def test_validate_bad_node_refs(self):
        index = self.make_index(2)
        trans = self.get_transport()
        content = trans.get_bytes('index')
        # change the options line to end with a rather than a parseable number
        new_content = content[:-2] + 'a\n\n'
        trans.put_bytes('index', new_content)
        self.assertRaises(errors.BadIndexOptions, index.validate)

    def test_validate_missing_end_line_empty(self):
        index = self.make_index(2)
        trans = self.get_transport()
        content = trans.get_bytes('index')
        # truncate the last byte
        trans.put_bytes('index', content[:-1])
        self.assertRaises(errors.BadIndexData, index.validate)

    def test_validate_missing_end_line_nonempty(self):
        index = self.make_index(2, [('key', ([], []), '')])
        trans = self.get_transport()
        content = trans.get_bytes('index')
        # truncate the last byte
        trans.put_bytes('index', content[:-1])
        self.assertRaises(errors.BadIndexData, index.validate)

    def test_validate_empty(self):
        index = self.make_index()
        index.validate()

    def test_validate_no_refs_content(self):
        index = self.make_index(nodes=[('key', (), 'value')])
        index.validate()
