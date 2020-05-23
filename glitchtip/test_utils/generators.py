from model_bakery import baker
from model_bakery.random_gen import gen_slug


def currency_code():
    return "USD"


baker.generators.add("organizations.fields.SlugField", gen_slug)
baker.generators.add("djstripe.fields.StripeCurrencyCodeField", currency_code)
