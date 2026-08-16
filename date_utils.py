"""Single source of truth for date conversions between ThoughtSpot and
Salesforce representations.

Confirmed against a live /searchdata call (queried with an explicit .daily
bucket): date columns come back as Unix epoch seconds, e.g. 1546300800 ->
2019-01-01. These helpers normalize to that canonical epoch-seconds int and
convert it to Salesforce's expected YYYY-MM-DD, while still tolerating a few
alternate string formats and passing anything unrecognized through as-is —
so a format that doesn't fit shows up as a visible error downstream instead
of failing silently.

Sentinel/out-of-range epochs (e.g. 1900-01-01 = -2208988800, used in the
source for "no expiry") convert to None rather than a bogus Salesforce date,
and the conversion avoids datetime.fromtimestamp() so it can't raise OSError
on Windows for negative/far-future values.
"""
from datetime import datetime, date, timezone, timedelta

_STRING_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Plausible window for a real membership/certification date. Values outside it
# are source-system sentinels (confirmed: epoch -2208988800 = 1900-01-01 is used
# for "no expiry" on some PMP records) or garbage — treated as "no date" rather
# than pushed to Salesforce. Also avoids datetime.fromtimestamp(), which raises
# OSError on Windows for negative / far-future epochs.
_MIN_EPOCH = 0            # 1970-01-01
_MAX_EPOCH = 7258118400   # ~2200-01-01


def to_epoch_seconds(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, datetime):
        return int(value.replace(tzinfo=timezone.utc).timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())
    if isinstance(value, str):
        for fmt in _STRING_FORMATS:
            try:
                return int(
                    datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
                )
            except ValueError:
                continue
    return value


def is_plausible_date_epoch(epoch) -> bool:
    """True only for an int epoch inside the plausible date window. Rejects
    None, bools, and source sentinels (e.g. 1900-01-01 = -2208988800) / garbage
    — so callers that compare raw epochs (e.g. the currency filter) treat a
    sentinel the same way to_salesforce_date() does: as "no real date".
    """
    return (
        isinstance(epoch, int)
        and not isinstance(epoch, bool)
        and _MIN_EPOCH <= epoch <= _MAX_EPOCH
    )


def to_salesforce_date(value):
    epoch = to_epoch_seconds(value)
    if epoch in (None, "") or isinstance(epoch, bool) or not isinstance(epoch, int):
        return epoch
    if epoch < _MIN_EPOCH or epoch > _MAX_EPOCH:
        return None  # sentinel (e.g. 1900-01-01 "no expiry") / out-of-range
    return (_EPOCH + timedelta(seconds=epoch)).date().isoformat()
