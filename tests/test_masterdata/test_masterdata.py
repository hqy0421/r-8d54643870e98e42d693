from decimal import Decimal

from agate import MasterDataBatch, MasterDataStore, Table
from agate.data_types import Boolean, Number, Text
from agate.exceptions import DanglingIdentityLinkError, DuplicateProfileError, MasterDataError, UnknownProfileError
from agate.masterdata.types import Native, infer_data_type
from agate.testcase import AgateTestCase


def profile_table(rows, column_names=('profile_id', 'name', 'email', 'phone', 'active'),
                  column_types=None, text_cast_nulls=True):
    if column_types is None:
        text = Text(cast_nulls=text_cast_nulls)
        column_types = [Number(), text, text, text, Boolean()]
    return Table(rows, list(column_names), list(column_types))


def order_table(rows, column_names=('order_id', 'profile_id', 'amount'),
                column_types=(Text(), Number(), Number())):
    return Table(rows, list(column_names), list(column_types))


class MasterDataTestCase(AgateTestCase):
    def setUp(self):
        self.crm = profile_table([
            (12, 'Acme Ltd', 'alice@acme.com', None, 0),
            (13, 'Beta Corp', None, '010-1111', False),
        ])
        self.erp = profile_table([
            (17, 'Acme Limited', 'alice@acme.com', '010-2222', None),
        ])
        self.web = profile_table([
            (30, '', 'alice@web.example', '010-3333', True),
        ], text_cast_nulls=False)
        self.orders = order_table([
            ('A-1', 12, 100),
            ('A-2', 17, 200),
            ('A-3', 30, 250),
            ('A-4', 13, 300),
            ('A-5', 99, 400),
        ])

    def make_store(self, **kwargs):
        kwargs.setdefault('key', 'profile_id')
        kwargs.setdefault('source_priority', ['crm', 'erp', 'web'])
        return MasterDataStore(self.orders, **kwargs)

    def seed(self, store):
        store.add_profiles(self.crm, source='crm')
        store.add_profiles(self.erp, source='erp')
        store.add_profiles(self.web, source='web')


class TestGrouping(MasterDataTestCase):
    def test_singleton_groups(self):
        store = self.make_store()
        self.seed(store)

        self.assertEqual(len(store.groups), 4)
        for profile_id, members in store.groups.items():
            self.assertEqual(members, (Decimal(profile_id),))

    def test_undirected_links_form_groups(self):
        store = self.make_store()
        self.seed(store)

        store.add_links([(17, 12)])
        store.add_links([(30, 17)])

        self.assertEqual(store.groups, {12: (12, 17, 30), 13: (13,)})
        self.assertEqual(store.master_ids, (12, 13))

    def test_master_is_minimum_profile_id(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(30, 12), (17, 12)])

        self.assertEqual(store.master_id_for(30), 12)
        self.assertEqual(store.master_id_for(17), 12)
        self.assertEqual(store.master_id_for(12), 12)

    def test_master_id_preserves_native_type(self):
        profiles = profile_table(
            [(1, None), (2, 'B')],
            column_names=('profile_id', 'name'),
            column_types=[Number(), Text()],
        )
        store = MasterDataStore(key='profile_id')
        store.add_profiles(profiles, source='s')
        store.add_links([(2, 1)])

        self.assertEqual(list(store.groups), [Decimal('1')])
        self.assertIsInstance(list(store.groups)[0], Decimal)

    def test_link_endpoints_may_be_strings(self):
        profiles = profile_table(
            [(10, 'A'), (11, 'B')],
            column_names=('profile_id', 'name'),
            column_types=[Number(), Text()],
        )
        store = MasterDataStore(key='profile_id')
        store.add_profiles(profiles, source='s')
        store.add_links([('11', '10')])

        self.assertEqual(store.groups, {10: (10, 11)})

    def test_unknown_link_endpoint_rejected(self):
        store = self.make_store()
        self.seed(store)

        with self.assertRaises(DanglingIdentityLinkError) as e:
            store.add_links([(12, 777)])

        self.assertEqual(e.exception.links, ((12, 777),))
        self.assertEqual(len(store.links), 0)

    def test_empty_store(self):
        store = MasterDataStore()

        self.assertEqual(store.groups, {})
        self.assertEqual(len(store.master_table().rows), 0)


class TestCoalescing(MasterDataTestCase):
    def test_source_priority(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        master = store.master_record(12)
        self.assertEqual(master['members'], (12, 17, 30))
        self.assertEqual(master['values']['name'], ('Acme Ltd', 'crm'))
        self.assertEqual(master['values']['phone'], ('010-2222', 'erp'))
        self.assertEqual(master['values']['email'], ('alice@acme.com', 'crm'))

    def test_zero_false_and_empty_string_are_values(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        master = store.master_record(12)
        self.assertEqual(master['values']['active'], (False, 'crm'))

        # The empty string from the web source is a value, but crm has higher
        # priority, so only a standalone web profile shows it.
        web_only = store.master_record(30)
        self.assertEqual(web_only, None)

        store.remove_links([(17, 30)])
        web_only = store.master_record(30)
        self.assertEqual(web_only['values']['name'], ('', 'web'))

    def test_zero_is_not_missing(self):
        profiles = profile_table(
            [(7, 0), (8, 5)],
            column_names=('profile_id', 'score'),
            column_types=[Number(), Number()],
        )
        store = MasterDataStore(source_priority=['a'])
        store.add_profiles(profiles, source='a')
        store.add_links([(7, 8)])

        self.assertEqual(store.master_record(7)['values']['score'], (0, 'a'))

    def test_false_is_not_missing(self):
        store = self.make_store(source_priority=['crm', 'erp', 'web'])
        store.add_profiles(self.crm, source='crm')
        store.add_profiles(self.erp, source='erp')
        store.add_links([(17, 12)])

        # crm says active=False, erp has no value; False must win over the
        # missing erp cell.
        self.assertEqual(store.master_record(12)['values']['active'], (False, 'crm'))

    def test_missing_value_falls_through_priority(self):
        store = self.make_store()
        store.add_profiles(self.crm, source='crm')
        store.add_profiles(self.erp, source='erp')
        store.add_links([(17, 12)])

        # crm phone is None, erp phone is a value.
        self.assertEqual(store.master_record(12)['values']['phone'], ('010-2222', 'erp'))

    def test_field_priority_overrides_source_priority(self):
        store = self.make_store(field_priority={'name': ['erp', 'crm']})
        self.seed(store)
        store.add_links([(17, 12)])

        self.assertEqual(store.master_record(12)['values']['name'], ('Acme Limited', 'erp'))
        # Other fields still follow the global order.
        self.assertEqual(store.master_record(12)['values']['phone'], ('010-2222', 'erp'))

    def test_unlisted_source_sorts_last(self):
        other = profile_table([(40, 'Zeta')], column_names=('profile_id', 'name'),
                              column_types=[Number(), Text()])
        store = self.make_store(source_priority=['crm', 'erp'])
        store.add_profiles(other, source='other')
        store.add_profiles(self.crm, source='crm')
        store.add_links([(40, 12)])

        self.assertEqual(store.master_record(12)['values']['name'], ('Acme Ltd', 'crm'))

    def test_provenance_source_column(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        table = store.master_table()
        row = table.rows[0]
        self.assertEqual(row['master_id'], 12)
        self.assertEqual(row['name'], 'Acme Ltd')
        self.assertEqual(row['name__source'], 'crm')
        self.assertEqual(row['active'], False)
        self.assertEqual(row['active__source'], 'crm')

    def test_include_sources_false(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])

        table = store.master_table(include_sources=False)
        self.assertColumnNames(table, ['master_id', 'name', 'email', 'phone', 'active'])


class TestNativeTypes(MasterDataTestCase):
    def test_mixed_column_keeps_native_types(self):
        profiles = Table(
            [(1, True, 'x'), (5, 'maybe', 0)],
            ['profile_id', 'flag', 'count'],
            [Number(), Native(), Native()],
        )
        store = MasterDataStore(key='profile_id')
        store.add_profiles(profiles, source='a')
        store.add_links([(1, 5)])

        values = store.master_record(1)['values']
        self.assertEqual(values['flag'], (True, 'a'))
        self.assertEqual(values['count'], ('x', 'a'))

        table = store.master_table()
        # Each column is homogeneous on its own, so standard agate types are
        # used and the native values are preserved by their casts.
        self.assertIsInstance(table.column_types[1], Boolean)
        self.assertIs(table.rows[0]['flag'], True)
        self.assertEqual(table.rows[0]['count'], 'x')

    def test_infer_data_type(self):
        self.assertIsInstance(infer_data_type([None, None]), Text)
        self.assertIsInstance(infer_data_type([1, 2]), Number)
        self.assertIsInstance(infer_data_type([True, False]), Boolean)
        self.assertIsInstance(infer_data_type(['a', 'b']), Text)
        self.assertIsInstance(infer_data_type([1, 'a']), Native)
        self.assertIsInstance(infer_data_type([True, 1]), Native)

    def test_native_preserves_values(self):
        native = Native()
        self.assertIs(native.cast(0), 0)
        self.assertIs(native.cast(False), False)
        self.assertEqual(native.cast(''), '')


class TestTables(MasterDataTestCase):
    def test_master_table(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        table = store.master_table()
        self.assertEqual(len(table.rows), 2)
        self.assertColumnNames(table, [
            'master_id',
            'name', 'name__source',
            'email', 'email__source',
            'phone', 'phone__source',
            'active', 'active__source',
        ])

    def test_orders_table_is_complete_and_ordered(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        table = store.orders_table()
        self.assertEqual(len(table.rows), len(self.orders.rows))
        self.assertEqual([r['order_id'] for r in table.rows], ['A-1', 'A-2', 'A-3', 'A-4', 'A-5'])
        self.assertEqual([r['profile_id'] for r in table.rows], list(self.orders.columns['profile_id']))

        # Orders of every group member share the master attributes.
        self.assertEqual(table.rows[0]['master_id'], 12)
        self.assertEqual(table.rows[1]['master_id'], 12)
        self.assertEqual(table.rows[2]['master_id'], 12)
        self.assertEqual(table.rows[3]['master_id'], 13)
        self.assertIsNone(table.rows[4]['master_id'])

        # Last order is unresolved: null attributes, but still present.
        self.assertIsNone(table.rows[4]['name'])
        self.assertIsNone(table.rows[4]['name__source'])

    def test_orders_table_column_collision(self):
        orders = Table(
            [('A-1', 12, 'original')],
            ['order_id', 'profile_id', 'name'],
            [Text(), Number(), Text()],
        )
        store = MasterDataStore(orders, key='profile_id')
        store.add_profiles(self.crm, source='crm')

        table = store.orders_table()
        self.assertIn('name', table.column_names)
        self.assertIn('master_name', table.column_names)
        self.assertEqual(table.rows[0]['name'], 'original')
        self.assertEqual(table.rows[0]['master_name'], 'Acme Ltd')
        self.assertEqual(table.rows[0]['master_name__source'], 'crm')

    def test_orders_without_orders_table(self):
        store = MasterDataStore(key='profile_id')
        store.add_profiles(self.crm, source='crm')

        with self.assertRaises(MasterDataError):
            store.orders_table()


class TestIncremental(MasterDataTestCase):
    def test_unresolved_order_attached_after_backfill(self):
        store = self.make_store()
        self.seed(store)

        before = store.orders_table()
        self.assertIsNone(before.rows[4]['master_id'])

        late = profile_table([(99, 'Late Co', None, None, None)])
        result = store.add_profiles(late, source='crm')

        self.assertEqual(result.unresolved_orders, 0)
        after = store.orders_table()
        self.assertEqual(after.rows[4]['master_id'], 99)
        self.assertEqual(after.rows[4]['name'], 'Late Co')

    def test_correction_does_not_rescan_unrelated_orders(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        correction = profile_table([(12, 'Acme LLC', None, None, None)])
        result = store.correct_profiles(correction, source='crm', fields=['name'])

        # Group 12 has three orders; the other two orders are untouched.
        self.assertEqual(result.orders_scanned, 3)
        self.assertLess(result.orders_scanned, len(self.orders.rows))
        self.assertEqual(store.master_record(12)['values']['name'], ('Acme LLC', 'crm'))

    def test_correction_recomputes_only_affected_master(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])

        correction = profile_table([(12, 'Acme LLC', None, None, None)])
        result = store.correct_profiles(correction, source='crm', fields=['name'])

        self.assertEqual(result.masters_updated, 1)

    def test_group_result_reports_counts(self):
        store = self.make_store()
        result = store.add_profiles(self.crm, source='crm')

        self.assertEqual(result.profiles_added, 2)
        self.assertEqual(result.profiles_corrected, 0)
        self.assertEqual(result.profiles_deleted, 0)
        self.assertEqual(result.orders_scanned, 2)
        self.assertEqual(result.orders_updated, 2)
        self.assertIsNotNone(result.snapshot)


class TestSplitting(MasterDataTestCase):
    def test_removing_bridge_link_splits_group(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        result = store.remove_links([(17, 30)])

        self.assertEqual(store.groups, {12: (12, 17), 13: (13,), 30: (30,)})
        self.assertGreater(result.masters_updated, 0)

        orders = store.orders_table()
        self.assertEqual(orders.rows[0]['master_id'], 12)
        self.assertEqual(orders.rows[1]['master_id'], 12)
        self.assertEqual(orders.rows[2]['master_id'], 30)
        self.assertEqual(orders.rows[2]['name'], '')

    def test_split_recomputes_surviving_master(self):
        # Master 1 keeps its id after the split, but loses the members that
        # supplied its values, so its record must be recomputed.
        profiles = profile_table(
            [(1, None), (2, 'N2'), (3, 'N3')],
            column_names=('profile_id', 'name'),
            column_types=[Number(), Text()],
        )
        orders = order_table([('o1', 1), ('o2', 2), ('o3', 3)])
        store = MasterDataStore(orders, key='profile_id')
        store.add_profiles(profiles, source='s')
        store.add_links([(1, 2), (2, 3)])

        self.assertEqual(store.master_record(1)['values']['name'], ('N2', 's'))

        store.remove_links([(1, 2)])

        self.assertEqual(store.groups, {1: (1,), 2: (2, 3)})
        self.assertEqual(store.master_record(1)['values']['name'], (None, None))
        self.assertEqual(store.master_record(2)['values']['name'], ('N2', 's'))

        orders_table = store.orders_table()
        self.assertIsNone(orders_table.rows[0]['name'])
        self.assertEqual(orders_table.rows[1]['master_id'], 2)
        self.assertEqual(orders_table.rows[2]['master_id'], 2)

    def test_unrelated_orders_not_rescanned_on_split(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])
        other = profile_table([(200, 'Other')], column_names=('profile_id', 'name'),
                              column_types=[Number(), Text()])
        store.add_profiles(other, source='crm')
        store2 = MasterDataStore(order_table([
            ('A-1', 12, 100), ('A-2', 17, 200), ('A-6', 200, 600),
        ]), key='profile_id')
        store2.add_profiles(self.crm, source='crm')
        store2.add_profiles(self.erp, source='erp')
        store2.add_profiles(other, source='crm')
        store2.add_links([(17, 12)])

        result = store2.remove_links([(17, 12)])

        # A-6 belongs to profile 200 and must not be scanned.
        self.assertEqual(result.orders_scanned, 2)

    def test_remove_link_accepts_reversed_endpoints(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(12, 17)])

        result = store.remove_links([(17, 12)])

        self.assertEqual(result.links_removed, 1)
        self.assertEqual(store.links, ())

    def test_add_link_is_idempotent(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(12, 17)])

        result = store.add_links([(17, 12)])

        self.assertEqual(result.links_added, 0)
        self.assertEqual(store.links, ((12, 17),))

    def test_delete_profile_splits(self):
        profiles = profile_table(
            [(1, 'A'), (2, 'B'), (3, 'C')],
            column_names=('profile_id', 'name'),
            column_types=[Number(), Text()],
        )
        orders = order_table([('o1', 1), ('o2', 2), ('o3', 3)])
        store = MasterDataStore(orders, key='profile_id')
        store.add_profiles(profiles, source='s')
        store.add_links([(1, 2), (2, 3)])

        batch = MasterDataBatch()
        batch.delete_profiles([2])
        batch.remove_links([(1, 2), (2, 3)])
        result = store.apply_batch(batch)

        self.assertEqual(result.profiles_deleted, 1)
        self.assertEqual(store.groups, {1: (1,), 3: (3,)})
        table = store.orders_table()
        self.assertIsNone(table.rows[1]['master_id'])


class TestBatches(MasterDataTestCase):
    def test_three_sources_same_batch(self):
        batch = MasterDataBatch()
        batch.add_profiles(self.crm, source='crm')
        batch.add_profiles(self.erp, source='erp')
        batch.add_profiles(self.web, source='web')
        batch.add_links([(17, 12), (30, 17)])

        store = self.make_store()
        result = store.apply_batch(batch)

        self.assertEqual(result.profiles_added, 4)
        self.assertEqual(result.links_added, 2)
        self.assertEqual(store.groups, {12: (12, 17, 30), 13: (13,)})

    def test_three_sources_with_correction_and_deletion(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        correction = profile_table([(13, 'Beta Corporation', None, None, None)])
        batch = MasterDataBatch()
        batch.correct_profiles(correction, source='erp', fields=['name'])
        batch.delete_profiles([30])
        batch.remove_links([(17, 30)])

        result = store.apply_batch(batch)

        self.assertEqual(result.profiles_corrected, 1)
        self.assertEqual(result.profiles_deleted, 1)
        self.assertEqual(result.links_removed, 1)
        self.assertEqual(store.master_record(13)['values']['name'], ('Beta Corporation', 'erp'))
        self.assertEqual(store.groups, {12: (12, 17), 13: (13,)})

    def test_delete_same_profile_twice_rejected(self):
        store = self.make_store()
        self.seed(store)
        before = store.groups

        with self.assertRaises(UnknownProfileError):
            store.delete_profiles([12, 12])

        self.assertEqual(store.groups, before)

    def test_delete_unknown_profile_rejected(self):
        store = self.make_store()
        self.seed(store)

        with self.assertRaises(UnknownProfileError) as e:
            store.delete_profiles([777])

        self.assertEqual(e.exception.profile_ids, (777,))

    def test_correct_unknown_profile_rejected(self):
        store = self.make_store()
        self.seed(store)

        correction = profile_table([(777, 'Nobody', None, None, None)])
        with self.assertRaises(UnknownProfileError):
            store.correct_profiles(correction, source='crm')

    def test_duplicate_profile_rejected(self):
        store = self.make_store()
        store.add_profiles(self.crm, source='crm')

        with self.assertRaises(DuplicateProfileError) as e:
            store.add_profiles(self.crm, source='erp')

        self.assertEqual(e.exception.profile_ids, (Decimal('12'),))

    def test_duplicate_profile_in_one_batch_rejected(self):
        profiles = profile_table([
            (50, 'A', None, None, None),
            (50, 'B', None, None, None),
        ])
        store = MasterDataStore(key='profile_id')

        with self.assertRaises(DuplicateProfileError):
            store.add_profiles(profiles, source='s')

    def test_dangling_link_rejects_whole_batch(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])
        before_groups = store.groups
        before_versions = len(store.history)

        batch = MasterDataBatch()
        batch.add_profiles(profile_table([(500, 'New', None, None, None)]), source='crm')
        batch.add_links([(500, 999)])

        with self.assertRaises(DanglingIdentityLinkError):
            store.apply_batch(batch)

        # Nothing at all committed.
        self.assertEqual(store.groups, before_groups)
        self.assertEqual(len(store.history), before_versions)
        self.assertIsNone(store.profile(500))
        self.assertEqual(store.links, ((12, 17),))

    def test_delete_leaving_dangling_link_rejected(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])
        before = store.groups

        with self.assertRaises(DanglingIdentityLinkError) as e:
            store.delete_profiles([17])

        self.assertEqual(e.exception.links, ((12, 17),))
        self.assertEqual(store.groups, before)

    def test_apply_batch_kwargs(self):
        store = self.make_store()
        result = store.apply_batch(
            additions=self.crm,
            source='crm',
        )
        self.assertEqual(result.profiles_added, 2)

    def test_apply_batch_rejects_non_batch(self):
        store = self.make_store()
        with self.assertRaises(MasterDataError):
            store.apply_batch('nope')

    def test_unknown_kwargs(self):
        store = self.make_store()
        with self.assertRaises(TypeError):
            store.apply_batch(additions=self.crm, bogus=1)


class TestSchema(MasterDataTestCase):
    def test_correction_adds_new_field_to_every_master(self):
        profiles = Table([(1, 'A'), (2, 'B')], ['profile_id', 'name'], [Number(), Text()])
        store = MasterDataStore(key='profile_id')
        store.add_profiles(profiles, source='a')

        correction = Table([(2, 'x@b.com')], ['profile_id', 'email'], [Number(), Text()])
        result = store.correct_profiles(correction, source='b')

        self.assertEqual(store.fields, ('name', 'email'))
        self.assertEqual(result.masters_updated, 2)
        self.assertEqual(store.master_record(1)['values']['email'], (None, None))
        self.assertEqual(store.master_record(2)['values']['email'], ('x@b.com', 'b'))
        self.assertColumnNames(store.master_table(), [
            'master_id',
            'name', 'name__source',
            'email', 'email__source',
        ])

    def test_fields_argument_sets_column_order(self):
        store = self.make_store(fields=['phone', 'name'])
        self.seed(store)

        self.assertEqual(store.fields, ('phone', 'name'))
        self.assertColumnNames(store.master_table(), [
            'master_id',
            'phone', 'phone__source',
            'name', 'name__source',
        ])

    def test_check_after_schema_change(self):
        profiles = Table([(1, 'A'), (2, 'B')], ['profile_id', 'name'], [Number(), Text()])
        store = MasterDataStore(key='profile_id')
        store.add_profiles(profiles, source='a')
        correction = Table([(2, 'x@b.com')], ['profile_id', 'email'], [Number(), Text()])
        store.correct_profiles(correction, source='b')

        self.assertTrue(store.check(full=True).ok)


class TestSnapshots(MasterDataTestCase):
    def test_every_batch_appends_snapshot(self):
        store = self.make_store()
        self.assertEqual(store.history, ())

        self.seed(store)

        self.assertEqual([s.version for s in store.history], [0, 1, 2])
        self.assertEqual(store.latest_snapshot.version, 2)

    def test_snapshot_keeps_old_tables(self):
        store = self.make_store()
        self.seed(store)

        before = store.latest_snapshot
        before_table = before.master_table()
        before_rows = [tuple(r) for r in before_table.rows]

        store.add_links([(17, 12)])
        store.correct_profiles(
            profile_table([(12, 'Changed', None, None, None)]),
            source='crm', fields=['name'],
        )

        self.assertEqual([tuple(r) for r in before.master_table().rows], before_rows)
        self.assertEqual(before.master_table().rows[0]['name'], 'Acme Ltd')
        self.assertEqual(store.master_table().rows[0]['name'], 'Changed')

    def test_snapshot_order_table(self):
        store = self.make_store()
        self.seed(store)
        snapshot = store.latest_snapshot

        table = snapshot.orders_table()
        self.assertIsNone(table.rows[4]['master_id'])

    def test_snapshot_links_and_groups(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])

        snapshot = store.latest_snapshot
        self.assertEqual(snapshot.links, ((Decimal(12), Decimal(17)),))
        self.assertEqual(snapshot.groups, {
            Decimal(12): (Decimal(12), Decimal(17)),
            Decimal(13): (Decimal(13),),
            Decimal(30): (Decimal(30),),
        })
        self.assertEqual(
            set(snapshot.profiles),
            {Decimal(12), Decimal(13), Decimal(17), Decimal(30)},
        )

    def test_restore(self):
        store = self.make_store()
        self.seed(store)
        snapshot = store.latest_snapshot

        store.add_links([(17, 12)])
        self.assertEqual(len(store.groups), 3)

        store.restore(snapshot)

        self.assertEqual(store.groups, {12: (12,), 13: (13,), 17: (17,), 30: (30,)})
        # Restore itself is recorded.
        self.assertEqual(store.latest_snapshot.description, 'restored version 2')

    def test_restore_by_version(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])

        store.restore(0)

        self.assertEqual(store.groups, {12: (12,), 13: (13,)})

    def test_restore_invalid(self):
        store = self.make_store()
        with self.assertRaises(MasterDataError):
            store.restore('nope')

    def test_orders_table_does_not_mutate_input(self):
        rows = [tuple(r) for r in self.orders.rows]
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])
        store.orders_table()

        self.assertEqual([tuple(r) for r in self.orders.rows], rows)


class TestCheck(MasterDataTestCase):
    def test_clean_full_check(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12), (30, 17)])

        check = store.check(full=True)
        self.assertTrue(check.ok)
        self.assertEqual(check.profiles, 4)
        self.assertEqual(check.groups, 2)
        self.assertEqual(check.orders, 5)
        self.assertEqual(check.orders_scanned, 5)

    def test_links_only_check_skips_orders(self):
        store = self.make_store()
        self.seed(store)

        check = store.check(full=False)
        self.assertTrue(check.ok)
        self.assertEqual(check.orders_scanned, 0)
        self.assertEqual(check.masters_updated, 0)

    def test_check_detects_stale_state(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])

        store._master_records[12]['values']['name'] = ('WRONG', 'x')

        self.assertTrue(store.check(full=False).ok)
        check = store.check(full=True)
        self.assertFalse(check.ok)
        self.assertEqual(check.masters_updated, 1)

    def test_check_repair(self):
        store = self.make_store()
        self.seed(store)
        store.add_links([(17, 12)])
        store._master_records[12]['values']['name'] = ('WRONG', 'x')

        check = store.check(full=True, repair=True)

        self.assertTrue(check.ok)
        self.assertEqual(store.master_record(12)['values']['name'], ('Acme Ltd', 'crm'))

    def test_check_reports_dangling(self):
        store = self.make_store()
        self.seed(store)
        store._links.add((12, 999))

        check = store.check(full=False)
        self.assertFalse(check.ok)
        self.assertEqual(check.dangling_links, ((12, 999),))

    def test_check_strict_raises(self):
        store = self.make_store()
        self.seed(store)
        store._links.add((12, 999))

        with self.assertRaises(DanglingIdentityLinkError):
            store.check(full=False, strict=True)

    def test_check_bool(self):
        store = self.make_store()
        self.seed(store)
        self.assertTrue(store.check(full=False))


class TestValidation(MasterDataTestCase):
    def test_orders_requires_key_column(self):
        orders = Table([('A-1', 100)], ['order_id', 'amount'], [Text(), Number()])

        with self.assertRaises(MasterDataError):
            MasterDataStore(orders, key='profile_id')

    def test_profile_table_requires_key_column(self):
        store = MasterDataStore(key='profile_id')

        with self.assertRaises(MasterDataError):
            store.add_profiles(Table([(1, 'A')], ['id', 'name'], [Number(), Text()]), source='s')

    def test_null_profile_id_rejected(self):
        store = MasterDataStore(key='profile_id')
        profiles = Table([(None, 'A')], ['profile_id', 'name'], [Number(), Text()])

        with self.assertRaises(MasterDataError):
            store.add_profiles(profiles, source='s')

    def test_orders_must_be_table(self):
        with self.assertRaises(MasterDataError):
            MasterDataStore('nope')

    def test_store_with_initial_profiles_and_links(self):
        store = MasterDataStore(
            self.orders, key='profile_id', profiles=self.crm,
            links=[(13, 12)], source_column=None,
        )

        self.assertEqual(store.groups, {12: (12, 13)})

    def test_store_with_initial_links_only(self):
        with self.assertRaises(DanglingIdentityLinkError):
            MasterDataStore(key='profile_id', links=[(1, 2)])

    def test_missing_source_column_is_ignored(self):
        profiles = Table([(1, 'A')], ['profile_id', 'name'], [Number(), Text()])
        store = MasterDataStore(key='profile_id', source_column='source')
        result = store.add_profiles(profiles, source='s')

        self.assertEqual(result.profiles_added, 1)
        self.assertEqual(store.master_record(1)['values']['name'], ('A', 's'))

    def test_source_column_per_row(self):
        profiles = Table(
            [(1, 'A', 'crm'), (2, 'B', 'erp')],
            ['profile_id', 'name', 'source'],
            [Number(), Text(), Text()],
        )
        store = MasterDataStore(key='profile_id', source_priority=['crm', 'erp'])
        store.add_profiles(profiles)
        store.add_links([(1, 2)])

        self.assertEqual(store.master_record(1)['values']['name'], ('A', 'crm'))

    def test_profile_and_master_record_missing(self):
        store = self.make_store()
        self.assertIsNone(store.profile(12))
        self.assertIsNone(store.master_record(12))
        self.assertIsNone(store.master_id_for(12))

    def test_correction_clears_field_with_none(self):
        store = self.make_store()
        self.seed(store)

        correction = profile_table([(12, None, None, None, None)])
        store.correct_profiles(correction, source='crm', fields=['name'])

        self.assertEqual(store.master_record(12)['values']['name'], (None, None))

    def test_correction_field_not_mentioned_is_untouched(self):
        store = self.make_store()
        self.seed(store)

        correction = profile_table([(12, 'Acme Ltd 2', None, None, None)])
        store.correct_profiles(correction, source='crm', fields=['name'])

        self.assertEqual(store.profile(12)['fields']['email'], ('alice@acme.com', 'crm'))
