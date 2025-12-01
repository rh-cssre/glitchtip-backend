from django.db.models import Lookup
from django.db.models.fields import Field


@Field.register_lookup
class Ilike(Lookup):
    lookup_name = "ilike"

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = lhs_params + rhs_params
        # Use the native PostgreSQL case-insensitive LIKE operator
        return "%s ILIKE %s" % (lhs, rhs), params
