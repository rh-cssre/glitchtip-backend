"""Port of sentry.api.endpoints.debug_files.DifAssembleEndpoint"""

import codecs
import io
import re
import tempfile
import zipfile
from hashlib import sha1

from asgiref.sync import sync_to_async
from django.core.files import File as DjangoFile
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from gt_rust.symbolic import normalize_debug_id
from ninja import File as NinjaFile
from ninja import Router
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja.pagination import paginate

from apps.files.models import File, FileBlob
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.decorators import optional_slash
from glitchtip.api.permissions import has_permission

from .models import DebugInformationFile
from .schema import AssemblePayload, DebugFileSchema
from .tasks import DIF_STATE_CREATED, DIF_STATE_NOT_FOUND, DIF_STATE_OK, difs_assemble

MAX_UPLOAD_BLOB_SIZE = 32 * 1024 * 1024  # 32MB


router = Router()


@optional_slash(
    router,
    "post",
    "projects/{slug:organization_slug}/{slug:project_slug}/files/difs/assemble/",
)
@has_permission(["project:write", "project:admin", "project:releases"])
async def difs_assemble_api(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    payload: AssemblePayload,
):
    organization = await aget_object_or_404(
        Organization, slug=organization_slug.lower(), users=request.auth.user_id
    )
    project = await aget_object_or_404(
        Project, slug=project_slug.lower(), organization=organization
    )

    responses = {}

    files = payload.root.items()

    for checksum, file in files:
        chunks = file.chunks
        name = file.name
        debug_id = file.debug_id
        debug_file = await (
            DebugInformationFile.objects.filter(
                project=project, file__checksum=checksum
            )
            .select_related("file")
            .afirst()
        )

        if debug_file is not None:
            responses[checksum] = {
                "state": DIF_STATE_OK,
                "missingChunks": [],
            }
            continue

        existed_chunks = [
            file_blob
            async for file_blob in FileBlob.objects.filter(
                checksum__in=chunks
            ).values_list("checksum", flat=True)
        ]

        missing_chunks = list(set(chunks) - set(existed_chunks))

        if len(missing_chunks) != 0:
            responses[checksum] = {
                "state": DIF_STATE_NOT_FOUND,
                "missingChunks": missing_chunks,
            }
            continue

        responses[checksum] = {"state": DIF_STATE_CREATED, "missingChunks": []}
        await difs_assemble.aenqueue(project.id, name, checksum, chunks, debug_id)

    return responses


@optional_slash(
    router,
    "post",
    "projects/{slug:organization_slug}/{slug:project_slug}/reprocessing/",
)
@has_permission(["project:write", "project:admin", "project:releases"])
async def project_reprocessing(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
):
    """
    Not implemented. It is a dummy API to keep `sentry-cli upload-dif` happy
    """
    return


def extract_proguard_id(name: str):
    match = re.search("proguard/([-a-fA-F0-9]+).txt", name)
    if match is None:
        return
    return match.group(1)


# A ProGuard/R8 mapping file's class records always look like
# `originalClassName -> obfuscatedClassName:` at column 0; member records
# (fields/methods) are indented under a class record and never appear before
# one. So the first non-comment, non-blank line of a real mapping is always
# a class record — checking just that line is enough to tell a mapping file
# apart from arbitrary text, without parsing the rest of the file.
_PROGUARD_CLASS_RECORD_RE = re.compile(r"^\S+ -> \S+:$")

# Real mapping files put the first class record within the first few header
# comment lines, so a small prefix is always enough to find it. Bounding the
# sniff avoids decoding the whole file to `str`, which for a mapping in the
# tens of MBs would itself allocate a comparable amount of RAM just to look
# at the first line.
_PROGUARD_SNIFF_BYTES = 64 * 1024


def extract_proguard_metadata(content: bytes):
    """Cheaply sniff-check that this looks like a ProGuard mapping file.

    This intentionally avoids ProguardMapper.open(), which eagerly builds a
    full in-memory class/method index over the entire file just to answer
    yes/no here — for a mapping file in the tens of MBs that index build can
    itself use several hundred MB of RAM, which is wasted work since nothing
    below actually uses the parsed mapper. Real deobfuscation still goes
    through ProguardMapper in StacktraceProcessor.resolve_proguard_stacktrace.
    """
    head = content[:_PROGUARD_SNIFF_BYTES].removeprefix(codecs.BOM_UTF8)

    for line in head.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        try:
            decoded = stripped.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if _PROGUARD_CLASS_RECORD_RE.match(decoded) is None:
            return None
        break

    return {"arch": "any", "feature": "mapping"}


async def create_dif_from_read_only_file(proguard_file, project, proguard_id, filename):
    with tempfile.NamedTemporaryFile("br+") as tmp:
        content = proguard_file.read()
        metadata = extract_proguard_metadata(content)
        if metadata is None:
            return None
        checksum = sha1(content).hexdigest()
        size = len(content)

        blob = await FileBlob.objects.filter(checksum=checksum).afirst()

        if blob is None:
            tmp.write(content)
            tmp.flush()
            blob = FileBlob(checksum=checksum, size=size)
            await sync_to_async(blob.blob.save)(filename, DjangoFile(tmp))
            await blob.asave()

        fileobj = await File.objects.filter(checksum=checksum).afirst()

        if fileobj is None:
            fileobj = File()
            fileobj.name = filename
            fileobj.headers = {}
            fileobj.checksum = checksum
            fileobj.size = size
            fileobj.blob = blob
            await fileobj.asave()

        dif = await DebugInformationFile.objects.filter(
            file__checksum=checksum, project=project
        ).afirst()

        if dif is None:
            dif = DebugInformationFile()
            dif.name = filename
            dif.project = project
            dif.file = fileobj
            dif.data = {
                "arch": metadata["arch"],
                "debug_id": normalize_debug_id(proguard_id),
                "symbol_type": "proguard",
                "features": ["mapping"],
            }
            await dif.asave()

        result = {
            "id": dif.id,
            "debugId": proguard_id,
            "cpuName": "any",
            "objectName": "proguard-mapping",
            "symbolType": "proguard",
            "size": size,
            "sha1": checksum,
            "data": {"features": ["mapping"]},
            "headers": {"Content-Type": "text/x-proguard+plain"},
            "dateCreated": fileobj.created,
        }

        return result


@optional_slash(
    router,
    "get",
    "projects/{slug:organization_slug}/{slug:project_slug}/files/dsyms/",
    response=list[DebugFileSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:read", "project:write", "project:admin"])
async def list_dsyms(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    response: HttpResponse,
):
    organization = await aget_object_or_404(
        Organization, slug=organization_slug.lower(), users=request.auth.user_id
    )
    project = await aget_object_or_404(
        Project, slug=project_slug.lower(), organization=organization
    )

    return DebugInformationFile.objects.filter(project=project).select_related("file")


@optional_slash(
    router, "post", "projects/{slug:organization_slug}/{slug:project_slug}/files/dsyms/"
)
@has_permission(["project:write", "project:admin", "project:releases"])
async def dsyms(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    file: UploadedFile = NinjaFile(...),
):
    organization = await aget_object_or_404(
        Organization, slug=organization_slug.lower(), users=request.auth.user_id
    )
    # self.check_object_permissions(request, organization)
    project = await aget_object_or_404(
        Project, slug=project_slug.lower(), organization=organization
    )
    if file.size > MAX_UPLOAD_BLOB_SIZE:
        raise HttpError(
            400,
            "File size too large",
        )

    content = file.read()

    buffer = io.BytesIO(content)

    if zipfile.is_zipfile(buffer) is False:
        raise HttpError(400, "Invalid file type uploaded")

    results = []

    with zipfile.ZipFile(buffer) as uploaded_zip_file:
        for filename in uploaded_zip_file.namelist():
            proguard_id = extract_proguard_id(filename)
            if proguard_id is None:
                raise HttpError(400, "")

            with uploaded_zip_file.open(filename) as proguard_file:
                result = await create_dif_from_read_only_file(
                    proguard_file, project, proguard_id, filename
                )
                if result is None:
                    raise HttpError(
                        400,
                        "Invalid proguard mapping file uploaded",
                    )
                results.append(result)

    return results
