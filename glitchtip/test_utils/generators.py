from model_bakery import baker
from model_bakery.random_gen import gen_slug, gen_datetime, gen_integer


def currency_code():
    return "USD"


baker.generators.add("organizations.fields.SlugField", gen_slug)
baker.generators.add("djstripe.fields.StripeCurrencyCodeField", currency_code)
baker.generators.add("djstripe.fields.StripeIdField", gen_slug)
baker.generators.add("djstripe.fields.StripeDateTimeField", gen_datetime)
baker.generators.add("djstripe.fields.StripeQuantumCurrencyAmountField", gen_integer)
