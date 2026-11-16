"""
Customer master data maintenance for agate.

A :class:`.MasterDataStore` keeps a set of *customer profiles* (the same
customer may have several profiles, one per source system), a set of
*explicit identity links* between those profiles, and an *orders* table.

Identity links are undirected.  The connected components they induce are the
customer groups.  The master profile of a group is the profile with the
smallest profile id, which is used as the master id.  Every field of the
master profile is taken from the group's profiles according to a configurable
priority, and the source of the chosen value is recorded next to it.  Only
:code:`None` is considered missing: :code:`0`, :code:`False` and the empty
string :code:`''` are ordinary values.

Batches of additions, corrections and deletions can be applied atomically.
If a batch would leave an identity link pointing at a profile that does not
exist, the whole batch is rejected with :class:`.DanglingIdentityLinkError`
and nothing is changed.  Removing a bridge link can split a group, so a
batch recomputes group membership instead of only touching the edited rows;
orders attached to every affected group are rewritten.  Unrelated orders are
not rescanned unless :meth:`.MasterDataStore.check` is called with
``full=True``.

All values keep their native Python type and the input tables are never
modified.  Every successful batch appends an immutable
:class:`.MasterDataSnapshot`, so the previous master and order tables remain
available.
"""

import copy
import numbers
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from agate.data_types import Text
from agate.exceptions import DanglingIdentityLinkError, DuplicateProfileError, MasterDataError, UnknownProfileError
from agate.masterdata.types import infer_data_type
from agate.rows import Row

__all__ = [
    'MasterDataStore',
    'MasterDataBatch',
    'MasterDataSnapshot',
    'MasterDataCheck',
    'BatchResult',
]


def _normalize_source(source):
    if source is None:
        return None
    return str(source)


def _is_missing(value):
    """
    Master data treats only ``None`` as missing.

    ``0``, ``False`` and ``''`` are values and must survive merging.
    """
    return value is None


def _id_key(value):
    """
    A total-order key for profile ids that may mix strings and numbers.

    Numbers sort before strings; equal numeric values of different Python
    types compare equal.  Anything else sorts by type name and repr, so the
    ordering is deterministic even for unusual ids.
    """
    if value is None:
        return (0, '')
    if isinstance(value, bool):
        return (1, Decimal(int(value)))
    if isinstance(value, numbers.Number):
        try:
            return (1, Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return (1, Decimal(0))
    if isinstance(value, str):
        return (2, value)
    return (3, type(value).__name__, repr(value))


def _key_variants(value):
    """
    Return the values that should be considered the same profile id.

    Profile ids are allowed to arrive as text from one source and as a number
    from another.  Exact values always match; numeric-looking strings also
    match the equivalent number.  The exact value comes first so it always
    wins a lookup.
    """
    variants = [value]

    def add(candidate):
        if candidate not in variants:
            variants.append(candidate)

    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped != value:
            add(stripped)
        try:
            number = Decimal(stripped)
        except (InvalidOperation, ValueError):
            pass
        else:
            add(number)
            if number == number.to_integral_value():
                add(int(number))
    elif isinstance(value, numbers.Number) and not isinstance(value, bool):
        number = Decimal(str(value))
        if number == number.to_integral_value():
            integral = int(number)
            add(integral)
            add(str(integral))
        add(str(value))

    return tuple(variants)


class _IdIndex:
    """
    Map any spelling of a profile id to the key actually stored in a profiles
    mapping.

    Python considers ``2``, ``Decimal('2')`` and (by our variant rules)
    ``'2'`` to be the same customer, but a dict lookup only yields the value,
    not the stored key.  This index keeps the stored key so that master ids
    and link endpoints always use one native representation.
    """
    def __init__(self, profiles):
        self._index = {}
        for key in profiles:
            self.add(key)

    def add(self, key):
        self._index.setdefault(key, key)
        for variant in _key_variants(key):
            self._index.setdefault(variant, key)

    def resolve(self, value):
        for variant in _key_variants(value):
            if variant in self._index:
                return self._index[variant]
        return None


def _lookup(mapping, value, index=None):
    """
    Resolve ``value`` to the key stored in ``mapping``, or ``None``.

    Pass a prepared :class:`_IdIndex` when resolving several values against
    the same mapping.
    """
    if index is None:
        index = _IdIndex(mapping)
    return index.resolve(value)


def _link_variants(link):
    return tuple(frozenset(_key_variants(endpoint)) for endpoint in link)


def _same_link(left, right):
    """
    Whether two links are the same edge, ignoring endpoint order and id type.
    """
    left_variants = _link_variants(left)
    right_variants = _link_variants(right)

    def overlaps(a, b):
        return bool(a & b)

    return (
        overlaps(left_variants[0], right_variants[0])
        and overlaps(left_variants[1], right_variants[1])
    ) or (
        overlaps(left_variants[0], right_variants[1])
        and overlaps(left_variants[1], right_variants[0])
    )


def _members_by_master(groups):
    """
    Invert a ``profile_id -> master_id`` mapping into ordered member lists.
    """
    members = OrderedDict()
    for profile_id, master in groups.items():
        members.setdefault(master, []).append(profile_id)
    return members


class BatchResult:
    """
    The outcome of a successful :meth:`.MasterDataStore.apply_batch`.

    :ivar profiles_added: Number of profiles added.
    :ivar profiles_corrected: Number of profiles corrected.
    :ivar profiles_deleted: Number of profiles deleted.
    :ivar links_added: Number of identity links added.
    :ivar links_removed: Number of identity links removed.
    :ivar groups_changed: Whether group membership or any master record was
        recomputed.
    :ivar masters_updated: Number of master profiles recomputed.
    :ivar orders_scanned: Number of orders examined for reattachment.
    :ivar orders_updated: Number of orders whose master attributes changed.
    :ivar unresolved_orders: Number of orders that still have no profile.
    :ivar snapshot: The :class:`.MasterDataSnapshot` created by the batch.
    """
    def __init__(self, profiles_added=0, profiles_corrected=0, profiles_deleted=0,
                 links_added=0, links_removed=0, groups_changed=False,
                 masters_updated=0, orders_scanned=0, orders_updated=0,
                 unresolved_orders=0, snapshot=None):
        self.profiles_added = profiles_added
        self.profiles_corrected = profiles_corrected
        self.profiles_deleted = profiles_deleted
        self.links_added = links_added
        self.links_removed = links_removed
        self.groups_changed = groups_changed
        self.masters_updated = masters_updated
        self.orders_scanned = orders_scanned
        self.orders_updated = orders_updated
        self.unresolved_orders = unresolved_orders
        self.snapshot = snapshot

    def _asdict(self):
        return {
            'profiles_added': self.profiles_added,
            'profiles_corrected': self.profiles_corrected,
            'profiles_deleted': self.profiles_deleted,
            'links_added': self.links_added,
            'links_removed': self.links_removed,
            'groups_changed': self.groups_changed,
            'masters_updated': self.masters_updated,
            'orders_scanned': self.orders_scanned,
            'orders_updated': self.orders_updated,
            'unresolved_orders': self.unresolved_orders,
        }

    def __repr__(self):
        counts = ', '.join('%s=%r' % item for item in self._asdict().items())
        return '<BatchResult: %s>' % counts


class MasterDataCheck:
    """
    The outcome of :meth:`.MasterDataStore.check`.

    :ivar ok: Whether the customer graph is consistent.
    :ivar dangling_links: Identity links whose endpoint is not a profile.
    :ivar profiles: Number of profiles.
    :ivar groups: Number of customer groups.
    :ivar orders: Number of orders.
    :ivar unresolved_orders: Number of orders without a profile.
    :ivar orders_scanned: Number of orders examined.
    :ivar masters_updated: Number of master records recomputed.
    """
    def __init__(self, ok, dangling_links, profiles, groups, orders,
                 unresolved_orders, orders_scanned, masters_updated):
        self.ok = ok
        self.dangling_links = tuple(dangling_links)
        self.profiles = profiles
        self.groups = groups
        self.orders = orders
        self.unresolved_orders = unresolved_orders
        self.orders_scanned = orders_scanned
        self.masters_updated = masters_updated

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return '<MasterDataCheck ok=%r profiles=%i groups=%i orders=%i unresolved=%i>' % (
            self.ok, self.profiles, self.groups, self.orders, self.unresolved_orders)


class MasterDataSnapshot:
    """
    An immutable view of the store at one commit.

    Snapshots hold copies of the profiles, identity links, master profiles and
    order attachments, as well as the field schema and the order table.  The
    tables returned by :meth:`master_table` and :meth:`orders_table` therefore
    keep the state they had when the snapshot was taken.

    :ivar version: A zero-based sequence number.
    :ivar description: An optional description supplied to the batch.
    """
    def __init__(self, version, description, fields, profiles, links, groups,
                 master_records, order_attachments, orders_table,
                 master_id_column, order_key, source_tables, master_rows):
        self.version = version
        self.description = description
        self.fields = tuple(fields)
        self._profiles = profiles
        self._links = links
        self._groups = groups
        self._master_records = master_records
        self._order_attachments = order_attachments
        self._orders_table = orders_table
        self._master_id_column = master_id_column
        self._order_key = order_key
        self._source_tables = source_tables
        self._master_rows = master_rows

    @property
    def profiles(self):
        """A copy of the profile ids in this snapshot."""
        return tuple(self._profiles.keys())

    @property
    def links(self):
        """The identity links in this snapshot, as ordered pairs."""
        return tuple(tuple(sorted(link, key=_id_key)) for link in self._links)

    @property
    def source_tables(self):
        """The input tables contributed up to this snapshot."""
        return tuple(self._source_tables)

    @property
    def groups(self):
        """A mapping of master id to the sorted member profile ids."""
        return OrderedDict(
            (master, tuple(sorted(members, key=_id_key)))
            for master, members in self._groups.items()
        )

    @property
    def master_rows(self):
        """A tuple of dicts, one per master profile, in master-id order."""
        return self._master_rows

    def master_table(self, include_sources=True):
        """
        Build the master profile table for this snapshot.

        :param include_sources:
            Whether to include a ``<field>__source`` column per field.
        :returns:
            A new :class:`.Table`.
        """
        from agate.table import Table

        column_names, column_types, rows = _build_master_table(
            self.fields, self._master_records, self._master_id_column, include_sources
        )

        return Table(rows, column_names, column_types, _is_fork=True)

    def orders_table(self, include_sources=True):
        """
        Build the orders table, completed with master profile attributes, for
        this snapshot.

        Orders whose profile is not known are present with a null master id
        and null master attributes.

        :param include_sources:
            Whether to include a ``<field>__source`` column per field.
        :returns:
            A new :class:`.Table`.
        """
        from agate.table import Table

        column_names, column_types, rows = _build_orders_table(
            self._orders_table, self.fields, self._order_attachments,
            self._master_id_column, self._order_key, include_sources
        )

        return Table(rows, column_names, column_types,
                     row_names=self._orders_table.row_names, _is_fork=True)


def _source_column_name(field):
    return '%s__source' % field


def _build_master_table(fields, master_records, master_id_column, include_sources):
    masters = sorted(master_records, key=_id_key)

    column_names = [master_id_column]
    for field in fields:
        column_names.append(field)
        if include_sources:
            column_names.append(_source_column_name(field))

    master_ids = [master for master in masters]
    values = {field: [] for field in fields}
    sources = {field: [] for field in fields}

    for master in masters:
        record = master_records[master]['values']
        for field in fields:
            value, source = record.get(field, (None, None))
            values[field].append(value)
            sources[field].append(source)

    column_types = [infer_data_type(master_ids)]
    for field in fields:
        column_types.append(infer_data_type(values[field]))
        if include_sources:
            column_types.append(Text())

    rows = []
    for i, master in enumerate(masters):
        row = [master]
        for field in fields:
            row.append(values[field][i])
            if include_sources:
                row.append(sources[field][i])
        rows.append(Row(row, column_names))

    return column_names, column_types, rows


def _build_orders_table(orders_table, fields, attachments, master_id_column, order_key, include_sources):
    order_column_names = list(orders_table.column_names)
    used = set(order_column_names)

    def output_name(name):
        candidate = name
        while candidate in used:
            candidate = 'master_%s' % candidate
        used.add(candidate)
        return candidate

    def source_name(field_name):
        return '%s__source' % field_name

    master_id_output = output_name(master_id_column)
    field_outputs = OrderedDict()
    source_outputs = OrderedDict()
    for field in fields:
        field_outputs[field] = output_name(field)
        if include_sources:
            source_outputs[field] = output_name(source_name(field_outputs[field]))

    column_names = order_column_names + [master_id_output]
    for field in fields:
        column_names.append(field_outputs[field])
        if include_sources:
            column_names.append(source_outputs[field])

    master_ids = []
    field_values = {field: [] for field in fields}
    field_sources = {field: [] for field in fields}

    for master, values, sources in attachments:
        master_ids.append(master)
        for field in fields:
            field_values[field].append(values.get(field))
            field_sources[field].append(sources.get(field))

    column_types = list(orders_table.column_types)
    column_types.append(infer_data_type(master_ids))
    for field in fields:
        column_types.append(infer_data_type(field_values[field]))
        if include_sources:
            column_types.append(Text())

    rows = []
    for i, order_row in enumerate(orders_table.rows):
        row = list(order_row)
        row.append(master_ids[i])
        for field in fields:
            row.append(field_values[field][i])
            if include_sources:
                row.append(field_sources[field][i])
        rows.append(Row(row, column_names))

    return column_names, column_types, rows


def _read_table_pairs(table, key, fields=None, source_column=None, default_source=None):
    """
    Yield ``(profile_id, {field: value}, source)`` for each row of ``table``.

    Only the requested ``fields`` are read; when ``fields`` is ``None`` every
    column except the key (and the source column) is read.
    """
    if key not in table.column_names:
        raise MasterDataError('Profile table has no column named "%s".' % key)

    if source_column is not None and source_column not in table.column_names:
        raise MasterDataError('Profile table has no column named "%s".' % source_column)

    if fields is None:
        fields = [name for name in table.column_names if name != key and name != source_column]
    else:
        for field in fields:
            if field not in table.column_names:
                raise MasterDataError('Profile table has no column named "%s".' % field)

    for row in table.rows:
        profile_id = row[key]
        if profile_id is None or profile_id == '':
            raise MasterDataError('Profile table contains a row with a null profile id.')

        if source_column is not None:
            source = row[source_column]
        else:
            source = default_source

        yield profile_id, {field: row[field] for field in fields}, _normalize_source(source)


class _AddProfiles:
    def __init__(self, table, key, source, source_column, fields):
        self.table = table
        self.key = key
        self.source = source
        self.source_column = source_column
        self.fields = fields


class _CorrectProfiles:
    def __init__(self, table, key, source, source_column, fields):
        self.table = table
        self.key = key
        self.source = source
        self.source_column = source_column
        self.fields = fields


class _DeleteProfiles:
    def __init__(self, profile_ids):
        self.profile_ids = tuple(profile_ids)


class MasterDataBatch:
    """
    A set of profile additions, corrections and deletions plus identity-link
    changes, to be applied atomically by
    :meth:`.MasterDataStore.apply_batch`.

    Additions, corrections and deletions may come from any number of source
    systems in the same batch.  Operations are applied in the order
    deletions, additions, corrections, then link changes.

    Example::

        batch = MasterDataBatch()
        batch.add_profiles(crm_table, key='id', source='crm')
        batch.correct_profiles(erp_table, key='id', source='erp')
        batch.delete_profiles([17])
        batch.add_links([(12, 17)])
        store.apply_batch(batch)
    """
    def __init__(self):
        self.additions = []
        self.corrections = []
        self.deletions = []
        self.links_add = []
        self.links_remove = []
        self.description = None

    def add_profiles(self, table, key='profile_id', source=None, source_column=None, fields=None):
        """
        Add every row of ``table`` as a new profile.

        :param table: A :class:`.Table`.
        :param key: The column containing the profile id.
        :param source: The source system name for every row.
        :param source_column: A column containing the source system per row.
        :param fields: The columns to store, defaulting to all but key/source.
        """
        self.additions.append(_AddProfiles(table, key, source, source_column, fields))
        return self

    def correct_profiles(self, table, key='profile_id', source=None, source_column=None, fields=None):
        """
        Correct existing profiles with the columns present in ``table``.

        A column that is present in ``table`` updates that field; a column
        that is absent leaves the stored value untouched.  A cell that is
        ``None`` clears the field.
        """
        self.corrections.append(_CorrectProfiles(table, key, source, source_column, fields))
        return self

    def delete_profiles(self, profile_ids):
        """
        Delete profiles by id.

        :param profile_ids: A sequence of profile ids.
        """
        self.deletions.append(_DeleteProfiles(profile_ids))
        return self

    def add_links(self, links):
        """
        Add explicit identity links.

        :param links: A sequence of ``(profile_id, profile_id)`` pairs.
        """
        self.links_add.extend(links)
        return self

    def remove_links(self, links):
        """
        Remove explicit identity links.

        :param links: A sequence of ``(profile_id, profile_id)`` pairs.
        """
        self.links_remove.extend(links)
        return self

    def describe(self, description):
        """Attach a description to the snapshot created by this batch."""
        self.description = description
        return self


class MasterDataStore:
    """
    Maintain customer master data for an orders table.

    :param orders:
        The original orders :class:`.Table`.  It is never modified.
    :param key:
        The name of the profile-id column in profile tables.
    :param order_key:
        The name of the profile-id column in ``orders``.  Defaults to ``key``.
    :param profiles:
        An optional initial profile :class:`.Table`.
    :param links:
        Optional initial identity links as ``(profile_id, profile_id)`` pairs.
    :param source_column:
        The default column in profile tables that names the source system.
    :param source_priority:
        A sequence of source system names, most trusted first.
    :param field_priority:
        A mapping of field name to a sequence of source system names, most
        trusted first, overriding ``source_priority`` for that field.  It may
        also be a plain sequence, in which case it applies to every field.
    :param master_id_column:
        The name of the master id column in the master and orders tables.
    :param fields:
        The field names (and their order) to maintain.  Defaults to every
        field seen in a profile.
    """
    def __init__(self, orders=None, key='profile_id', order_key=None, profiles=None, links=None,
                 source_column='source', source_priority=None, field_priority=None,
                 master_id_column='master_id', fields=None):
        if orders is not None and not hasattr(orders, 'column_names'):
            raise MasterDataError('orders must be an agate.Table.')

        self._orders = orders
        self._key = key
        self._order_key = order_key if order_key is not None else key
        self._source_column = source_column
        self._source_priority = tuple(source_priority) if source_priority is not None else None
        self._field_priority = self._normalize_field_priority(field_priority)
        self._master_id_column = master_id_column

        self._fields = list(fields) if fields is not None else []
        self._field_schema = list(self._fields)
        self._restrict_fields = fields is not None

        self._profiles = OrderedDict()
        self._links = set()
        self._groups = OrderedDict()
        self._master_records = OrderedDict()
        self._master_rows = []

        self._order_attachments = []
        self._orders_by_key = {}
        self._orders_by_master = {}
        self._source_tables = []
        self._history = []

        self._index_orders()

        if profiles is not None:
            batch = MasterDataBatch()
            batch.add_profiles(profiles, key=key,
                               source_column=self._resolve_source_column(profiles, None))
            if links:
                batch.add_links(links)
            self.apply_batch(batch)
        else:
            if links:
                batch = MasterDataBatch()
                batch.add_links(links)
                self.apply_batch(batch)
            else:
                self._rebuild()

    def _register_field(self, fields, field):
        """
        Add a newly-seen field to the schema unless the schema was fixed.

        When the store was constructed with ``fields``, that sequence is the
        complete schema and columns outside it are ignored.
        """
        if field in fields:
            return
        if self._restrict_fields and field not in self._field_schema:
            return
        fields.append(field)

    def _resolve_source_column(self, table, source_column):
        """
        Pick the source column for ``table``.

        An explicit column wins.  Otherwise the store's configured default is
        used when the table actually has that column, so a profiles table
        without source information is not rejected.
        """
        if source_column is not None:
            return source_column
        if self._source_column is not None and self._source_column in table.column_names:
            return self._source_column
        return None

    @staticmethod
    def _normalize_field_priority(field_priority):
        if field_priority is None:
            return None
        if isinstance(field_priority, dict):
            return OrderedDict(
                (field, tuple(priority)) for field, priority in field_priority.items()
            )
        return tuple(field_priority)

    # -- configuration and read-only views ---------------------------------

    @property
    def fields(self):
        """The field names maintained by this store, in column order."""
        return tuple(self._fields)

    @property
    def key(self):
        """The profile-id column name used by profile tables."""
        return self._key

    @property
    def order_key(self):
        """The profile-id column name used by the orders table."""
        return self._order_key

    @property
    def master_id_column(self):
        """The master id column name used in the master and orders tables."""
        return self._master_id_column

    @property
    def orders(self):
        """The original, unmodified orders table."""
        return self._orders

    @property
    def profiles(self):
        """A copy of the current profiles, keyed by profile id."""
        return copy.deepcopy(self._profiles)

    @property
    def links(self):
        """The current identity links, as sorted tuples."""
        return tuple(tuple(sorted(link, key=_id_key)) for link in self._links)

    @property
    def groups(self):
        """A mapping of master id to the sorted member profile ids."""
        members_by_master = OrderedDict()
        for profile_id, master in self._groups.items():
            members_by_master.setdefault(master, []).append(profile_id)
        return OrderedDict(
            (master, tuple(sorted(members, key=_id_key)))
            for master, members in members_by_master.items()
        )

    @property
    def master_ids(self):
        """The master ids of all customer groups, in ascending order."""
        return tuple(sorted(set(self._groups.values()), key=_id_key))

    @property
    def history(self):
        """All snapshots, oldest first."""
        return tuple(self._history)

    @property
    def latest_snapshot(self):
        """The most recent snapshot, or ``None`` before the first commit."""
        return self._history[-1] if self._history else None

    @property
    def source_tables(self):
        """Every table supplied to a batch, in order."""
        return tuple(self._source_tables)

    def profile(self, profile_id):
        """
        Return a copy of one profile, or ``None`` if it does not exist.

        The result has ``source`` and ``fields`` keys; each field maps to a
        ``(value, source)`` pair.
        """
        canonical = self._resolve_profile_id(profile_id)
        if canonical is None:
            return None
        return copy.deepcopy(self._profiles[canonical])

    def master_id_for(self, profile_id):
        """
        Return the master id of the group containing ``profile_id``, or
        ``None`` if the profile is unknown.
        """
        canonical = self._resolve_profile_id(profile_id)
        if canonical is None:
            return None
        return self._groups.get(canonical)

    def master_record(self, master_id):
        """
        Return a copy of one master record, or ``None`` if it does not exist.

        The result maps each field to a ``(value, source)`` pair and also has
        ``members`` and ``master_id`` keys.
        """
        canonical = self._resolve_profile_id(master_id)
        if canonical is None or canonical not in self._master_records:
            return None
        record = self._master_records[canonical]
        return {
            'master_id': canonical,
            'members': tuple(sorted(record['members'], key=_id_key)),
            'values': copy.deepcopy(record['values']),
        }

    # -- order indexing -----------------------------------------------------

    def _index_orders(self):
        self._orders_by_key = {}
        self._orders_by_master = {}
        self._order_attachments = []

        if self._orders is None:
            return

        if self._order_key not in self._orders.column_names:
            raise MasterDataError('Orders table has no column named "%s".' % self._order_key)

        # One attachment per order row, in row order.  Unresolved until a
        # batch supplies the profile.
        self._order_attachments = [(None, {}, {}) for _ in self._orders.rows]

        for i, row in enumerate(self._orders.rows):
            for variant in _key_variants(row[self._order_key]):
                self._orders_by_key.setdefault(variant, set()).add(i)

    def _order_rows_for_profile(self, profile_id):
        rows = set()
        for variant in _key_variants(profile_id):
            rows.update(self._orders_by_key.get(variant, ()))
        return rows

    def _order_rows_for_master(self, master_id):
        return set(self._orders_by_master.get(master_id, ()))

    # -- graph --------------------------------------------------------------

    def _resolve_profile_id(self, value):
        return _lookup(self._profiles, value)

    def _rebuild_groups(self):
        parent = {}

        def find(item):
            parent.setdefault(item, item)
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left, right):
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                if _id_key(left_root) <= _id_key(right_root):
                    parent[right_root] = left_root
                else:
                    parent[left_root] = right_root

        for profile_id in self._profiles:
            find(profile_id)

        index = _IdIndex(self._profiles)
        for left, right in self._links:
            resolved_left = index.resolve(left)
            resolved_right = index.resolve(right)
            if resolved_left is not None and resolved_right is not None:
                union(resolved_left, resolved_right)

        self._parent = parent

    # -- master resolution --------------------------------------------------

    def _priority_for(self, field):
        if self._field_priority is None:
            return self._source_priority or ()
        if isinstance(self._field_priority, dict):
            priority = self._field_priority.get(field)
            if priority is None:
                return self._source_priority or ()
            return priority
        return self._field_priority

    def _choose_value(self, field, members):
        priority = self._priority_for(field)
        ranks = {source: i for i, source in enumerate(priority)}

        best = None
        best_key = None

        for profile_id in members:
            entry = self._profiles[profile_id]['fields'].get(field)
            if entry is None:
                continue

            value, source = entry
            if _is_missing(value):
                continue

            rank = ranks.get(source, len(priority) + 1)
            candidate_key = (rank, _id_key(profile_id))

            if best_key is None or candidate_key < best_key:
                best_key = candidate_key
                best = (value, source)

        if best is None:
            return (None, None)

        return best

    def _record_for_members(self, master, members):
        values = OrderedDict()
        for field in self._fields:
            values[field] = self._choose_value(field, members)
        return {'master_id': master, 'members': tuple(members), 'values': values}

    def _master_row(self, master, record):
        row = [master]
        for field in self._fields:
            value, source = record['values'].get(field, (None, None))
            row.append(value)
            row.append(source)
        return row

    def _attachment_for(self, profile_key):
        profile_id = _lookup(self._profiles, profile_key)
        if profile_id is None:
            return (None, {}, {})
        master = self._groups.get(profile_id)
        record = self._master_records.get(master)
        if record is None:
            return (None, {}, {})
        values = {field: value for field, (value, source) in record['values'].items()}
        sources = {field: source for field, (value, source) in record['values'].items()}
        return (master, values, sources)

    def _refresh_order_attachments(self, rows):
        if self._orders is None:
            return 0

        changed = 0
        for i in rows:
            previous = self._order_attachments[i]
            current = self._attachment_for(self._orders.rows[i][self._order_key])
            if previous != current:
                changed += 1
                self._order_attachments[i] = current

            # Keep the master -> row index in sync.
            old_master = previous[0]
            new_master = current[0]
            if old_master != new_master:
                if old_master is not None and old_master in self._orders_by_master:
                    self._orders_by_master[old_master].discard(i)
                if new_master is not None:
                    self._orders_by_master.setdefault(new_master, set()).add(i)

        return changed

    # -- full rebuild -------------------------------------------------------

    def _rebuild(self):
        """
        Recompute every group, master record and order attachment.

        Used when the store is first populated, never on the incremental
        path.
        """
        self._rebuild_groups()

        self._groups = OrderedDict(
            (profile_id, self._master_id_of(profile_id)) for profile_id in self._profiles
        )

        members_by_master = _members_by_master(self._groups)

        self._master_records = OrderedDict()
        for master in sorted(members_by_master, key=_id_key):
            members = tuple(sorted(members_by_master[master], key=_id_key))
            self._master_records[master] = self._record_for_members(master, members)

        self._master_rows = [
            self._master_row(master, record) for master, record in self._master_records.items()
        ]

        if self._orders is not None:
            self._orders_by_master = {}
            for i, attachment in enumerate(self._order_attachments):
                master = attachment[0]
                if master is not None:
                    self._orders_by_master.setdefault(master, set()).add(i)

            self._refresh_order_attachments(range(len(self._order_attachments)))

    def _master_id_of(self, profile_id):
        root = profile_id
        seen = set()
        while self._parent.get(root, root) != root:
            if root in seen:
                break
            seen.add(root)
            root = self._parent[root]
        return root

    # -- batch application --------------------------------------------------

    def apply_batch(self, batch=None, **kwargs):
        """
        Apply a :class:`.MasterDataBatch` atomically.

        Instead of a batch object, the operation arguments can be passed
        directly::

            store.apply_batch(
                additions=crm_table,
                corrections=erp_table,
                deletions=[17],
                links_add=[(12, 17)],
                source='crm',
            )

        The whole batch is rejected with
        :class:`.DanglingIdentityLinkError` if any identity link would point at
        a profile that does not exist after the batch.  On rejection no
        profile, link, order or snapshot changes.

        :returns:
            A :class:`.BatchResult`.
        """
        if batch is None:
            batch = MasterDataBatch()

            additions = kwargs.pop('additions', None)
            corrections = kwargs.pop('corrections', None)
            deletions = kwargs.pop('deletions', None)
            links_add = kwargs.pop('links_add', None)
            links_remove = kwargs.pop('links_remove', None)
            source = kwargs.pop('source', None)
            source_column = kwargs.pop('source_column', None)
            key = kwargs.pop('key', self._key)

            if kwargs:
                raise TypeError('Unexpected arguments: %s' % ', '.join(sorted(kwargs)))

            if additions is not None:
                batch.add_profiles(additions, key=key, source=source, source_column=source_column)
            if corrections is not None:
                batch.correct_profiles(corrections, key=key, source=source, source_column=source_column)
            if deletions is not None:
                batch.delete_profiles(deletions)
            if links_add is not None:
                batch.add_links(links_add)
            if links_remove is not None:
                batch.remove_links(links_remove)

        if not isinstance(batch, MasterDataBatch):
            raise MasterDataError('apply_batch expects a MasterDataBatch.')

        # Work on copies so a rejected batch leaves no trace.
        profiles = copy.deepcopy(self._profiles)
        links = set(self._links)
        fields = list(self._fields)

        deleted = 0
        added = 0
        corrected = 0
        links_added = 0
        links_removed = 0
        added_ids = []
        corrected_ids = []
        deleted_ids = []
        tables = []

        index = _IdIndex(profiles)

        # Deletions.
        for operation in batch.deletions:
            for profile_id in operation.profile_ids:
                canonical = index.resolve(profile_id)
                if canonical is None:
                    raise UnknownProfileError([profile_id])
                del profiles[canonical]
                index = _IdIndex(profiles)
                deleted_ids.append(canonical)
                deleted += 1

        # Additions.
        for operation in batch.additions:
            tables.append(operation.table)
            source_column = self._resolve_source_column(operation.table, operation.source_column)
            seen = set()
            for profile_id, field_values, source in _read_table_pairs(
                    operation.table, operation.key, operation.fields,
                    source_column, operation.source):
                if index.resolve(profile_id) is not None or profile_id in seen:
                    raise DuplicateProfileError([profile_id])
                seen.add(profile_id)

                entry = OrderedDict()
                for field, value in field_values.items():
                    if self._restrict_fields and field not in self._field_schema:
                        continue
                    self._register_field(fields, field)
                    entry[field] = (value, source)

                profiles[profile_id] = {'source': source, 'fields': entry}
                index.add(profile_id)
                added_ids.append(profile_id)
                added += 1

        # Corrections.
        for operation in batch.corrections:
            tables.append(operation.table)
            source_column = self._resolve_source_column(operation.table, operation.source_column)
            for profile_id, field_values, source in _read_table_pairs(
                    operation.table, operation.key, operation.fields,
                    source_column, operation.source):
                canonical = index.resolve(profile_id)
                if canonical is None:
                    raise UnknownProfileError([profile_id])

                for field, value in field_values.items():
                    if self._restrict_fields and field not in self._field_schema:
                        continue
                    self._register_field(fields, field)
                    field_source = source if source is not None else profiles[canonical]['source']
                    profiles[canonical]['fields'][field] = (value, field_source)

                if canonical not in corrected_ids:
                    corrected_ids.append(canonical)
                corrected += 1

        # Link changes.  Endpoints may be profiles added in this same batch,
        # and the batch may create a link and its profiles together.
        for left, right in batch.links_add:
            resolved_left = index.resolve(left)
            resolved_right = index.resolve(right)
            if resolved_left is None or resolved_right is None:
                # Kept as supplied; the final validation below will reject the
                # whole batch if the profile never appears.
                links.add((left, right))
                links_added += 1
                continue

            canonical = self._canonical_pair(resolved_left, resolved_right)
            if not any(_same_link(canonical, existing) for existing in links):
                links.add(canonical)
                links_added += 1

        for left, right in batch.links_remove:
            for existing in list(links):
                if _same_link(existing, (left, right)):
                    links.discard(existing)
                    links_removed += 1

        # Validate the final graph before committing anything.
        dangling = []
        canonical_links = set()
        final_index = _IdIndex(profiles)
        for left, right in links:
            resolved_left = final_index.resolve(left)
            resolved_right = final_index.resolve(right)
            if resolved_left is None or resolved_right is None:
                dangling.append((left, right))
                continue
            canonical_links.add(self._canonical_pair(resolved_left, resolved_right))

        if dangling:
            raise DanglingIdentityLinkError(dangling)

        links = canonical_links

        # Commit profile/link state.
        old_groups = dict(self._groups)
        self._profiles = profiles
        self._links = links
        field_schema_changed = fields != self._field_schema
        self._fields = fields
        self._field_schema = list(fields)
        if tables:
            self._source_tables.extend(tables)

        self._rebuild_groups()

        new_groups = {}
        for profile_id in self._profiles:
            new_groups[profile_id] = self._master_id_of(profile_id)
        self._groups = new_groups

        # Compare group *membership*, not just master ids: splitting a bridge
        # link can leave a surviving group with the same master id but fewer
        # members, which can still change every field chosen for that master.
        old_members = {}
        for profile_id, master in old_groups.items():
            old_members.setdefault(master, set()).add(profile_id)
        new_members = {}
        for profile_id, master in new_groups.items():
            new_members.setdefault(master, set()).add(profile_id)

        affected_masters = set()
        for master in set(old_members) | set(new_members):
            if old_members.get(master) != new_members.get(master):
                affected_masters.add(master)

        # A field added by this batch must appear (usually null) in every
        # master record, so a schema change touches every master.
        if field_schema_changed:
            affected_masters.update(new_members)

        affected_pids = set(added_ids) | set(deleted_ids) | set(corrected_ids)
        for master in affected_masters:
            affected_pids.update(old_members.get(master, ()))
            affected_pids.update(new_members.get(master, ()))
        for profile_id in corrected_ids:
            master = new_groups.get(profile_id)
            if master is not None:
                affected_masters.add(master)

        # Recompute only the affected master records.
        new_records = OrderedDict(self._master_records)
        members_by_master = OrderedDict()
        for profile_id, master in self._groups.items():
            members_by_master.setdefault(master, []).append(profile_id)

        masters_changed = 0
        for master in affected_masters:
            previous = new_records.get(master)
            if master not in members_by_master:
                if previous is not None:
                    del new_records[master]
                    masters_changed += 1
                continue
            members = tuple(sorted(members_by_master[master], key=_id_key))
            record = self._record_for_members(master, members)
            if previous is None or previous['members'] != record['members'] \
                    or previous['values'] != record['values']:
                masters_changed += 1
            new_records[master] = record

        # Keep master rows in master-id order.
        ordered_records = OrderedDict()
        for master in sorted(new_records, key=_id_key):
            ordered_records[master] = new_records[master]
        self._master_records = ordered_records
        self._master_rows = [
            self._master_row(master, record) for master, record in ordered_records.items()
        ]

        # Reattach only orders in affected groups (and orders of deleted or
        # newly added profiles).  A split group changes master ids, so the
        # affected-profile set above already includes every member whose
        # master id changed.
        orders_scanned = 0
        orders_updated = 0
        if self._orders is not None:
            rows = set()
            for profile_id in affected_pids:
                rows |= self._order_rows_for_profile(profile_id)
            for master in affected_masters:
                rows |= self._order_rows_for_master(master)

            orders_scanned = len(rows)
            orders_updated = self._refresh_order_attachments(rows)

        unresolved_orders = sum(1 for attachment in self._order_attachments if attachment[0] is None)

        result = BatchResult(
            profiles_added=added,
            profiles_corrected=corrected,
            profiles_deleted=deleted,
            links_added=links_added,
            links_removed=links_removed,
            groups_changed=bool(affected_masters or added_ids or deleted_ids),
            masters_updated=masters_changed,
            orders_scanned=orders_scanned,
            orders_updated=orders_updated,
            unresolved_orders=unresolved_orders,
        )

        self._append_snapshot(getattr(batch, 'description', None))
        result.snapshot = self._history[-1]
        return result

    @staticmethod
    def _canonical_pair(left, right):
        if _id_key(left) <= _id_key(right):
            return (left, right)
        return (right, left)

    # convenience single-operation batches --------------------------------

    def add_profiles(self, table, key=None, source=None, source_column=None, fields=None):
        """
        Add profiles in a one-operation batch.  See
        :meth:`.MasterDataBatch.add_profiles`.
        """
        batch = MasterDataBatch()
        batch.add_profiles(table, key=key or self._key, source=source,
                           source_column=source_column, fields=fields)
        return self.apply_batch(batch)

    def correct_profiles(self, table, key=None, source=None, source_column=None, fields=None):
        """
        Correct profiles in a one-operation batch.  See
        :meth:`.MasterDataBatch.correct_profiles`.
        """
        batch = MasterDataBatch()
        batch.correct_profiles(table, key=key or self._key, source=source,
                               source_column=source_column, fields=fields)
        return self.apply_batch(batch)

    def delete_profiles(self, profile_ids):
        """
        Delete profiles in a one-operation batch.  See
        :meth:`.MasterDataBatch.delete_profiles`.
        """
        batch = MasterDataBatch()
        batch.delete_profiles(profile_ids)
        return self.apply_batch(batch)

    def add_links(self, links):
        """
        Add identity links in a one-operation batch.
        """
        batch = MasterDataBatch()
        batch.add_links(links)
        return self.apply_batch(batch)

    def remove_links(self, links):
        """
        Remove identity links in a one-operation batch.
        """
        batch = MasterDataBatch()
        batch.remove_links(links)
        return self.apply_batch(batch)

    # -- snapshots and validation ------------------------------------------

    def _append_snapshot(self, description):
        version = len(self._history)
        snapshot = MasterDataSnapshot(
            version=version,
            description=description,
            fields=tuple(self._fields),
            profiles=copy.deepcopy(self._profiles),
            links=frozenset(self._links),
            groups=copy.deepcopy(OrderedDict(
                (master, tuple(sorted(members, key=_id_key)))
                for master, members in _members_by_master(self._groups).items()
            )),
            master_records=copy.deepcopy(self._master_records),
            order_attachments=list(self._order_attachments),
            orders_table=self._orders,
            master_id_column=self._master_id_column,
            order_key=self._order_key,
            source_tables=list(self._source_tables),
            master_rows=copy.deepcopy(self._master_rows),
        )
        self._history.append(snapshot)
        return snapshot

    def snapshot(self, description=None):
        """
        Take an explicit snapshot of the current state without changing it.

        :returns:
            A :class:`.MasterDataSnapshot`.
        """
        return self._append_snapshot(description)

    def restore(self, snapshot):
        """
        Restore the state held by a snapshot.

        Restoring appends a new snapshot, so the intervening history is kept.

        :param snapshot:
            A :class:`.MasterDataSnapshot` or a snapshot version number.
        """
        if isinstance(snapshot, int):
            snapshot = self._history[snapshot]
        if not isinstance(snapshot, MasterDataSnapshot):
            raise MasterDataError('restore expects a MasterDataSnapshot or version number.')

        self._profiles = copy.deepcopy(snapshot._profiles)
        self._links = set(snapshot._links)
        self._fields = list(snapshot.fields)
        self._field_schema = list(snapshot.fields)
        self._master_records = copy.deepcopy(snapshot._master_records)
        self._master_rows = copy.deepcopy(snapshot._master_rows)
        self._order_attachments = list(snapshot._order_attachments)
        self._source_tables = list(snapshot._source_tables)

        self._rebuild_groups()
        self._groups = {
            profile_id: self._master_id_of(profile_id) for profile_id in self._profiles
        }

        self._orders_by_master = {}
        for i, attachment in enumerate(self._order_attachments):
            master = attachment[0]
            if master is not None:
                self._orders_by_master.setdefault(master, set()).add(i)

        return self._append_snapshot('restored version %i' % snapshot.version)

    def check(self, full=True, repair=False, strict=False):
        """
        Validate the customer graph and, optionally, every order attachment.

        This is the full-graph check.  Ordinary corrections use the
        incremental path instead and never rescan unrelated orders.

        :param full:
            If ``True`` (the default), recompute every customer group, master
            record and order attachment from the profiles and links and
            compare the result with the stored state, which detects any stale
            incremental state.  If ``False``, only validate the identity
            links.
        :param repair:
            If ``True``, commit the recomputed state when it differs.  By
            default the check is read-only.
        :param strict:
            If ``True``, raise :class:`.DanglingIdentityLinkError` when any
            identity link points at a missing profile.  Otherwise the report
            is returned with ``ok=False``.
        :returns:
            A :class:`.MasterDataCheck`.
        """
        dangling = []
        index = _IdIndex(self._profiles)
        for left, right in self._links:
            if index.resolve(left) is None or index.resolve(right) is None:
                dangling.append((left, right))

        if dangling and strict:
            raise DanglingIdentityLinkError(dangling)

        stale_masters = []
        stale_orders = 0
        stale_groups = False
        orders_scanned = 0

        if full:
            expected_groups, expected_records, expected_attachments = self._expected_state()

            stale_groups = (
                set(self._groups) != set(expected_groups)
                or any(expected_groups[p] != self._groups[p] for p in expected_groups)
            )

            for master, record in expected_records.items():
                current = self._master_records.get(master)
                if current is None or current['members'] != record['members'] \
                        or current['values'] != record['values']:
                    stale_masters.append(master)

            for master in self._master_records:
                if master not in expected_records:
                    stale_masters.append(master)

            if self._orders is not None:
                orders_scanned = len(expected_attachments)
                for i, attachment in enumerate(expected_attachments):
                    if self._order_attachments[i] != attachment:
                        stale_orders += 1

            if repair and (stale_groups or stale_masters or stale_orders):
                self._groups = expected_groups
                self._master_records = OrderedDict(
                    (master, expected_records[master])
                    for master in sorted(expected_records, key=_id_key)
                )
                self._master_rows = [
                    self._master_row(master, record)
                    for master, record in self._master_records.items()
                ]
                if self._orders is not None:
                    self._order_attachments = list(expected_attachments)
                    self._orders_by_master = {}
                    for i, attachment in enumerate(self._order_attachments):
                        if attachment[0] is not None:
                            self._orders_by_master.setdefault(attachment[0], set()).add(i)

                stale_groups = False
                stale_masters = []
                stale_orders = 0

        unresolved = sum(1 for attachment in self._order_attachments if attachment[0] is None)

        return MasterDataCheck(
            ok=not dangling and not stale_groups and not stale_masters and not stale_orders,
            dangling_links=dangling,
            profiles=len(self._profiles),
            groups=len(set(self._groups.values())),
            orders=len(self._order_attachments),
            unresolved_orders=unresolved,
            orders_scanned=orders_scanned,
            masters_updated=len(stale_masters),
        )

    def _expected_state(self):
        """
        Recompute groups, master records and order attachments from scratch
        without touching the store.
        """
        parent = {}
        index = _IdIndex(self._profiles)

        def find(item):
            parent.setdefault(item, item)
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left, right):
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                if _id_key(left_root) <= _id_key(right_root):
                    parent[right_root] = left_root
                else:
                    parent[left_root] = right_root

        for profile_id in self._profiles:
            find(profile_id)
        for left, right in self._links:
            resolved_left = index.resolve(left)
            resolved_right = index.resolve(right)
            if resolved_left is not None and resolved_right is not None:
                union(resolved_left, resolved_right)

        expected_groups = OrderedDict(
            (profile_id, find(profile_id)) for profile_id in self._profiles
        )

        members_by_master = OrderedDict()
        for profile_id, master in expected_groups.items():
            members_by_master.setdefault(master, []).append(profile_id)

        expected_records = OrderedDict()
        for master in sorted(members_by_master, key=_id_key):
            members = tuple(sorted(members_by_master[master], key=_id_key))
            expected_records[master] = self._record_for_members(master, members)

        expected_attachments = []
        if self._orders is not None:
            index = _IdIndex(self._profiles)
            for row in self._orders.rows:
                profile_id = index.resolve(row[self._order_key])
                master = expected_groups.get(profile_id) if profile_id is not None else None
                if master is None or master not in expected_records:
                    expected_attachments.append((None, {}, {}))
                    continue
                record = expected_records[master]
                expected_attachments.append((
                    master,
                    {field: value for field, (value, source) in record['values'].items()},
                    {field: source for field, (value, source) in record['values'].items()},
                ))

        return expected_groups, expected_records, expected_attachments

    # -- tables -------------------------------------------------------------

    def master_table(self, include_sources=True):
        """
        Build the master profile table.

        The first column is :attr:`master_id_column`.  Each field is followed
        by a ``<field>__source`` column naming the source system of the chosen
        value, when ``include_sources`` is true.

        :returns:
            A new :class:`.Table`.
        """
        from agate.table import Table

        column_names, column_types, rows = _build_master_table(
            self._fields, self._master_records, self._master_id_column, include_sources
        )
        return Table(rows, column_names, column_types, _is_fork=True)

    def orders_table(self, include_sources=True):
        """
        Build the orders table, completed with master profile attributes.

        Every original order row is present exactly once.  An order whose
        profile is unknown keeps a null master id and null master attributes;
        it is attached automatically once its profile is added.

        :returns:
            A new :class:`.Table`.
        """
        from agate.table import Table

        if self._orders is None:
            raise MasterDataError('This store was created without an orders table.')

        column_names, column_types, rows = _build_orders_table(
            self._orders, self._fields, self._order_attachments,
            self._master_id_column, self._order_key, include_sources
        )
        return Table(rows, column_names, column_types,
                     row_names=self._orders.row_names, _is_fork=True)
