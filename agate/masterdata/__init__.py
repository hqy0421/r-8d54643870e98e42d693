"""
Customer master data maintenance.

The same customer often has a different profile in every source system.  This
subpackage builds *customer groups* from explicit identity links, elects the
smallest profile id of each undirected component as the master id, and
coalesces every field from the group's profiles into a real master profile,
recording the source of each chosen value.

It also completes an orders table with the master profile's attributes and
reattaches old orders whenever a merge is confirmed or undone.  Changes are
applied in atomic batches; a batch that would leave an identity link pointing
at a missing profile is rejected in its entirety.

Only :code:`None` counts as missing: :code:`0`, :code:`False` and
:code:`''` are kept as values.

The usual entry point is :class:`.MasterDataStore`::

    from agate.masterdata import MasterDataStore

    store = MasterDataStore(orders, key='profile_id', source_priority=['crm', 'erp'])
    store.add_profiles(crm_profiles, source='crm')
    store.add_profiles(erp_profiles, source='erp')
    store.add_links([(12, 17)])
    store.master_table()
    store.orders_table()
"""

from agate.masterdata.store import BatchResult, MasterDataBatch, MasterDataCheck, MasterDataSnapshot, MasterDataStore
from agate.masterdata.types import Native, infer_data_type

__all__ = [
    'MasterDataStore',
    'MasterDataBatch',
    'MasterDataSnapshot',
    'MasterDataCheck',
    'BatchResult',
    'Native',
    'infer_data_type',
]
