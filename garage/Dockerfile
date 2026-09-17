# Garage with the dev config from this directory baked in, for use as a
# GitLab CI service and by compose.s3.yml.
#
# Why this image exists: GitLab CI services cannot mount files, and the
# upstream image is `scratch` -- it contains exactly one file, /garage, so a
# command override has no shell to write a config with. Garage will not start
# without /etc/garage.toml. Baking it in is the only way to run Garage as a
# CI service.
#
# This is a test fixture, not a supported GlitchTip artifact, and it is not
# rebuilt on a schedule. To move to a newer Garage, bump the tag here and:
#   docker build -t registry.gitlab.com/glitchtip/glitchtip-backend/garage:v2.4.1 garage/
#   docker push registry.gitlab.com/glitchtip/glitchtip-backend/garage:v2.4.1
# then update the tag in .gitlab-ci.yml and compose.s3.yml.
FROM dxflrs/garage:v2.4.1
COPY garage.toml /etc/garage.toml
