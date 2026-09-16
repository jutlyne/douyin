# DESUB dependency security gate

Evidence date: 2026-07-23
Status: **active; no dependency or tool was installed during Phase A.**

Before any Phase B or later package, model weight, binary, base image, font, SDK or
build tool is introduced, its exact version/digest must have a completed record with:

1. Maintainer and upstream source; abandoned packages are rejected.
2. License and license-text SHA-256, including redistribution compatibility.
3. Exact package/binary/image/model/font SHA-256 or immutable registry digest.
4. SBOM or a generated dependency inventory covering transitive components.
5. Current vendor advisories and recognized CVE databases checked on the install day.
6. Confirmation that the selected release is the latest patched compatible version.
7. A fail-closed decision: any known unmitigated CVE, uncertain version identity or
   unavailable advisory evidence blocks installation and requires user direction.
8. Installation command, source registry/domain and rollback/removal procedure.

Version ranges, mutable container tags, unversioned operating-system packages and
download-latest URLs are not acceptable production bindings. Existing prototype
dependencies are inventory evidence only; they are not grandfathered into Integrated
DESUB.

The current prototype Dockerfile was observed to request `torch==2.12.1+cu126`,
`torchvision==0.27.1+cu126`, `easyocr==1.7.2` and
`opencv-python-headless==5.0.0.93`, while installing `ffmpeg` and
`fonts-dejavu-core` from an unversioned operating-system repository. This is an
inventory observation, **not** a CVE/license approval or a production pin. Every one
of these components and its transitive/runtime libraries still needs an install-day
advisory check, immutable digest and license/SBOM record before reuse.

Phase A intentionally did not install `jsonschema`, FFmpeg, OCR/ASR packages, fonts,
model weights, SDKs or Cloud tooling. Contract checks use the Python standard library
already present in the workspace. Missing runtime assets remain Gate A blockers rather
than being downloaded without a security record.
