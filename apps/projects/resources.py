from django.core.validators import slug_re
from import_export import resources

from .models import RESERVED_PROJECT_SLUGS, Project, ProjectKey


class ProjectKeyResource(resources.ModelResource):
    class Meta:
        model = ProjectKey
        skip_unchanged = True
        fields = ("project", "label", "public_key")
        import_id_fields = (
            "project",
            "public_key",
        )


class ProjectResource(resources.ModelResource):
    class Meta:
        model = Project
        skip_unchanged = True
        fields = ("id", "created", "slug", "name", "organization", "platform")

    def do_instance_save(self, instance, is_create):
        # AutoSlugField rebuilds the slug from the name on insert; keep the imported one.
        slug = instance.slug
        instance.save()
        if (
            is_create
            and instance.slug != slug
            and slug_re.fullmatch(slug)
            and len(slug) <= Project._meta.get_field("slug").max_length
            and slug not in RESERVED_PROJECT_SLUGS
        ):
            Project.objects.filter(pk=instance.pk).update(slug=slug)
            instance.slug = slug
