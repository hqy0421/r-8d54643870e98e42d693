====================
Customer master data
====================

.. autosummary::
    :nosignatures:

    agate.MasterDataStore
    agate.MasterDataBatch
    agate.MasterDataSnapshot
    agate.MasterDataCheck
    agate.BatchResult
    agate.Native
    agate.infer_data_type

Overview
========

The same customer usually has a different profile in each source system.
:class:`.MasterDataStore` builds *customer groups* from explicit identity
links, elects the smallest profile id of each undirected component as the
master id, and coalesces every field from the group's profiles into a real
master profile.  The source of every chosen value is recorded in a
``<field>__source`` column.

Only :code:`None` counts as missing.  :code:`0`, :code:`False` and the empty
string :code:`''` are ordinary values and are never skipped.

.. code-block:: python

    import agate

    store = agate.MasterDataStore(
        orders,
        key='profile_id',
        source_priority=['crm', 'erp', 'web'],
    )

    store.add_profiles(crm_profiles, source='crm')
    store.add_profiles(erp_profiles, source='erp')
    store.add_profiles(web_profiles, source='web')

    store.add_links([(17, 12)])   # confirm that 17 and 12 are the same customer

    master = store.master_table()
    completed_orders = store.orders_table()

Identity links are undirected, so ``(17, 12)`` and ``(12, 17)`` are the same
link.  Every profile reachable through links belongs to one group, and every
field is taken from the group according to ``source_priority``.

Batches
=======

Changes are applied in atomic :class:`.MasterDataBatch` objects.  Additions,
corrections and deletions may come from any number of sources in one batch,
together with identity-link changes.

.. code-block:: python

    batch = agate.MasterDataBatch()
    batch.add_profiles(new_crm_profiles, source='crm')
    batch.add_profiles(new_erp_profiles, source='erp')
    batch.correct_profiles(web_corrections, source='web')
    batch.delete_profiles([30])
    batch.add_links([(17, 12)])
    batch.remove_links([(12, 30)])

    result = store.apply_batch(batch)

If the final state would contain an identity link whose endpoint is not a
profile, the entire batch is rejected with
:class:`.DanglingIdentityLinkError` and nothing is changed -- no profile, no
link, no order and no snapshot.

Orders
======

:meth:`.MasterDataStore.orders_table` returns every original order row exactly
once, completed with the master id and the master profile's attributes and
their sources.  An order whose profile is not known is kept with null master
attributes and is attached automatically once the profile is backfilled.

Removing a *bridge* link can split a group.  The store recomputes the group
membership of every affected profile and rewrites the masters and the orders
of every affected group; it does not merely edit the rows that were supplied
to the batch.

Incremental updates
===================

Ordinary corrections only recompute the customer groups and orders that the
change can affect.  :meth:`.MasterDataStore.check` performs a full check of
the customer graph and of every order attachment; it is read-only unless
``repair=True`` is passed.

Snapshots
=========

Every successful batch appends a :class:`.MasterDataSnapshot`, so the master
and orders tables of any earlier commit remain available:

.. code-block:: python

    old = store.history[0]
    old.master_table()
    old.orders_table()

    store.restore(old)

The input tables are never modified, and values keep their native Python
types.

.. autoclass:: agate.MasterDataStore
.. autoclass:: agate.MasterDataBatch
.. autoclass:: agate.MasterDataSnapshot
.. autoclass:: agate.MasterDataCheck
.. autoclass:: agate.BatchResult
.. autoclass:: agate.Native
.. autofunction:: agate.infer_data_type
