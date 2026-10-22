#!/usr/bin/env bash
# Build each image once per source tree and print its immutable digest reference.
#
# The tag is the git tree hash of the build inputs, not the commit: any commit that leaves those
# files unchanged maps to the same tag, so the image is reused instead of rebuilt. Rebuilding the
# same source is not byte-reproducible (timestamps), so reuse is what keeps the tested digest equal
# to the deployed one. The commit hash is added as a second tag for traceability.
#
# Usage: REGISTRY=ghcr.io/<owner> infra/scripts/publish_image.sh NAME CONTEXT DOCKERFILE KEY_PATH
set -euo pipefail

registry="${REGISTRY:?set REGISTRY, e.g. ghcr.io/minhpnz}"
name="$1" context="$2" dockerfile="$3" key_path="$4"

if [ -n "$(git status --porcelain -- "$key_path")" ]; then
  echo "uncommitted changes under $key_path: commit them so the tag matches the image content" >&2
  exit 1
fi

commit="$(git rev-parse HEAD)"
tree="$(git rev-parse "HEAD:$key_path")"
image="$registry/loadtest-demo/$name"
tree_ref="${image}:tree-${tree}"
source_url="$(git remote get-url origin | sed -e 's#^git@github.com:#https://github.com/#' -e 's#\.git$##')"

if docker buildx imagetools inspect --raw "$tree_ref" >/dev/null 2>&1; then
  echo "reuse $tree_ref (no changes under $key_path)" >&2
  docker pull -q --platform linux/amd64 "$tree_ref" >&2
else
  echo "build $tree_ref" >&2
  docker build --platform linux/amd64 \
    --label "org.opencontainers.image.source=$source_url" \
    --label "org.opencontainers.image.revision=$commit" \
    -f "$dockerfile" -t "$tree_ref" "$context" >&2
  docker push -q "$tree_ref" >&2
fi

# docker tag + push keeps the manifest byte-identical; `buildx imagetools create` may wrap it in
# an index with a different digest.
docker tag "$tree_ref" "${image}:${commit}"
docker push -q "${image}:${commit}" >&2

echo "${image}@sha256:$(docker buildx imagetools inspect --raw "$tree_ref" | shasum -a 256 | cut -d' ' -f1)"
