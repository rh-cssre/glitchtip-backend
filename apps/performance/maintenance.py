from .models import TransactionGroup


def cleanup_old_transaction_events():
    """Delete older events and associated data"""
    # Delete ~1k empty transaction groups at a time until less than 1k remain then delete the rest. Avoids memory overload.
    queryset = TransactionGroup.objects.filter(transactionevent=None).order_by("id")

    while True:
        try:
            empty_group_delimiter = queryset.values_list("id", flat=True)[
                1000:1001
            ].get()
            queryset.filter(id__lte=empty_group_delimiter).delete()
        except TransactionGroup.DoesNotExist:
            break

    queryset.delete()
