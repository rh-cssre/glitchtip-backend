import random
import string

from django_extensions.db.fields import AutoSlugField


class OrganizationSlugField(AutoSlugField):
    """
    Generate a random alphanumeric slug starting at length 2.
    Increase length by 1 if collision is found.
    """

    def slug_generator(self, original_slug, start):
        yield original_slug
        for i in range(start, self.max_unique_query_attempts):
            yield (
                original_slug
                + "-"
                + "".join(random.choices(string.ascii_lowercase + string.digits, k=i))
            )
        raise RuntimeError(
            "max slug attempts for %s exceeded (%s)"
            % (original_slug, self.max_unique_query_attempts)
        )
