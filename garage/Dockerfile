# Garage with the dev config baked in, for the CI service and compose.s3.yml.
# CI services cannot mount files and the upstream image is `scratch` with no
# shell, so baking it in is the only way to supply the /etc/garage.toml that
# Garage refuses to start without.
#
# Test fixture, not a shipped artifact. On a version bump rebuild and push to
# registry.gitlab.com/glitchtip/glitchtip-backend/garage, then update the tag
# here, in .gitlab-ci.yml and in compose.s3.yml.
FROM dxflrs/garage:v2.4.1
COPY garage.toml /etc/garage.toml
