"""Names a resolver mislabels without meaning to.

A review of the live index found three labelling faults, none in what the
index stores: a member missing from a class with an external base reported
as "no such attribute" (112 of 112 wrong), lambda and comprehension
variables and class-body attributes reported as "not found", and a column
read inside a query call recorded as a *call* to the column. Each shape is
here once.
"""

import sample_pkg as pkg
from . import helpers as h
from .middleware import Auditor
from .models import Order

HERE = __file__


class Ledger:
    rate = 3
    double = rate * 2          # the class body reads its own attribute


def apply_all(items):
    return list(map(lambda x: x * 2, items)) + [y for y in items if y]


def dump(a: Auditor):
    return a.model_dump()      # inherited from BaseModel, outside the codebase


def via_alias(text):
    return h.slugify(text), pkg.Order   # a module alias; a re-export through the package


def filtered(query):
    return query.where(Order.total == 3)   # reading a member inside a call is not a call


def described() -> str:
    return Order.total.__doc__.strip()     # the call is on strip; total is read


PAIRS = [("a", {"n": 1})]
for _kind, _spec in PAIRS:                 # module-level loop targets are module names
    LAST = _spec["n"]


class Window:
    def __init__(self, seconds: int):
        self.seconds = seconds

    def cutoff(self, now: float) -> float:
        return now - self.seconds          # set in __init__: an instance attribute, not absent


def deep(text):
    return pkg.helpers.slugify(text)       # a member of the submodule, not a missing one of pkg


KIND = Order.__name__                      # every class has it; not a missing member
WHERE = h.__file__                         # every module has it
