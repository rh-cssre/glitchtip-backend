from djstripe import webhooks


@webhooks.handler("customer.subscription.updated", "customer.subscription.created")
def update_subscription(event, **kwargs):
    """When the subscription is updated, immediately check for throttle adjustments"""
    # Avoid importing models during django app startup
    from apps.organizations_ext.tasks import check_organization_throttle

    check_organization_throttle.delay(event.customer.subscriber_id)
