"""
Data types used by :mod:`agate.masterdata`.

Master data columns are assembled from values that may come from different
source systems, so a column can legitimately mix native Python types (for
example a :class:`bool` from one system and a :class:`decimal.Decimal` from
another).  :class:`.Native` lets such a column pass through unchanged instead
of coercing every value to a single representation.
"""

import numbers
from datetime import date, datetime, timedelta

from agate.data_types.base import DataType
from agate.data_types.boolean import Boolean
from agate.data_types.date import Date
from agate.data_types.date_time import DateTime
from agate.data_types.number import Number
from agate.data_types.text import Text
from agate.data_types.time_delta import TimeDelta


class Native(DataType):
    """
    A :class:`.DataType` that preserves values exactly as they are supplied.

    This is used when a master data column contains more than one native
    Python type, where coercing to any single :class:`.DataType` would lose
    information.  In particular it keeps ``0``, ``False`` and ``''`` distinct
    from each other and from :code:`None`.
    """
    def cast(self, d):
        return d

    def csvify(self, d):
        return d

    def jsonify(self, d):
        return d


def infer_data_type(values):
    """
    Choose the most specific :class:`.DataType` that preserves every native
    value in ``values``.

    Homogeneous columns get the usual agate type (and therefore the usual
    aggregations).  Mixed columns fall back to :class:`.Native`, so no value
    is silently stringified or rounded.  :code:`None` never influences the
    choice.

    :param values:
        A sequence of native Python values.
    :returns:
        A :class:`.DataType` instance.
    """
    kinds = set()

    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add('boolean')
        elif isinstance(value, numbers.Number):
            kinds.add('number')
        elif isinstance(value, datetime):
            kinds.add('datetime')
        elif isinstance(value, date):
            kinds.add('date')
        elif isinstance(value, timedelta):
            kinds.add('timedelta')
        elif isinstance(value, str):
            kinds.add('text')
        else:
            kinds.add('other')

    if not kinds:
        return Text()

    if kinds == {'boolean'}:
        return Boolean()
    if kinds == {'number'}:
        return Number()
    if kinds == {'datetime'}:
        return DateTime()
    if kinds == {'date'}:
        return Date()
    if kinds == {'timedelta'}:
        return TimeDelta()
    if kinds == {'text'}:
        return Text()

    return Native()
