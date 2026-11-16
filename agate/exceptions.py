"""
This module contains various exceptions raised by agate.
"""


class DataTypeError(TypeError):  # pragma: no cover
    """
    A calculation was attempted with an invalid :class:`.DataType`.
    """
    pass


class UnsupportedAggregationError(TypeError):  # pragma: no cover
    """
    An :class:`.Aggregation` was attempted which is not supported.

    For example, if a :class:`.Percentiles` is applied to a :class:`.TableSet`.
    """
    pass


class CastError(Exception):  # pragma: no cover
    """
    A column value can not be cast to the correct type.
    """
    pass


class FieldSizeLimitError(Exception):  # pragma: no cover
    """
    A field in a CSV file exceeds the maximum length.

    This length may be the default or one set by the user.
    """
    def __init__(self, limit, line_number):
        super().__init__(
            'CSV contains a field longer than the maximum length of %i characters on line %i. Try raising the maximum '
            'with the field_size_limit parameter, or try setting quoting=csv.QUOTE_NONE.' % (limit, line_number)
        )


class MasterDataError(ValueError):
    """
    Base class for errors raised while maintaining customer master data with
    :class:`.MasterDataStore`.

    Subclasses carry structured details so that a rejected batch can be
    diagnosed without reparsing the message.
    """
    pass


class DanglingIdentityLinkError(MasterDataError):
    """
    A batch of customer profile changes left one or more identity links that
    point at profiles which do not exist.

    The whole batch is rejected: no profile, link, order or snapshot is
    modified. The offending links are available on :attr:`links`.

    :param links:
        A sequence of ``(source_profile_id, target_profile_id)`` pairs.
    """
    def __init__(self, links):
        self.links = tuple(links)
        super().__init__(
            'Batch rejected: %i identity link(s) point at profiles that do not exist: %s' % (
                len(self.links),
                ', '.join('%s -> %s' % link for link in self.links[:10])
                + (' ...' if len(self.links) > 10 else '')
            )
        )


class DuplicateProfileError(MasterDataError):
    """
    More than one profile with the same id was supplied in a single batch.

    :param profile_ids:
        A sequence of the duplicated profile ids.
    """
    def __init__(self, profile_ids):
        self.profile_ids = tuple(profile_ids)
        super().__init__('Batch rejected: duplicate profile id(s): %s' % ', '.join(str(p) for p in self.profile_ids))


class UnknownProfileError(MasterDataError):
    """
    A correction or deletion referred to a profile or link that does not
    exist.

    :param profile_ids:
        A sequence of the unknown profile ids.
    """
    def __init__(self, profile_ids):
        self.profile_ids = tuple(profile_ids)
        super().__init__('Batch rejected: unknown profile id(s): %s' % ', '.join(str(p) for p in self.profile_ids))
