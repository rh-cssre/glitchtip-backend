from model_bakery import baker
from model_bakery.random_gen import gen_slug

baker.generators.add("organizations.fields.SlugField", gen_slug)

