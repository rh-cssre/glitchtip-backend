"""Port of sentry.api.endpoints.debug_files.DifAssembleEndpoint"""

import io
import re
import tempfile
import zipfile
from hashlib import sha1

from asgiref.sync import sync_to_async
from django.core.files import File as DjangoFile
from django.shortcuts import aget_object_or_404
from ninja import File as NinjaFile
from ninja import Router
from ninja.errors import HttpError
from ninja.files import UploadedFile
from symbolic import ProguardMapper

from apps.files.models import File, FileBlob
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.utils import async_call_celery_task

from .models import DebugInformationFile
from .schema import AssemblePayload
from .tasks import DIF_STATE_CREATED, DIF_STATE_NOT_FOUND, DIF_STATE_OK, difs_assemble

MAX_UPLOAD_BLOB_SIZE = 32 * 1024 * 1024  # 32MB


router = Router()


@router.post(
    "projects/{slug:organization_slug}/{slug:project_slug}/files/difs/assemble/"
)
async def difs_assemble_api(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    payload: AssemblePayload,
):
    organization = await aget_object_or_404(
        Organization, slug=organization_slug.lower(), users=request.auth.user_id
    )
    await aget_object_or_404(
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
                project__slug=project_slug, file__checksum=checksum
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
        await async_call_celery_task(
            difs_assemble, project_slug, name, checksum, chunks, debug_id
        )

    return responses


@router.post("projects/{slug:organization_slug}/{slug:project_slug}/reprocessing/")
async def project_reprocessing(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
):
    """
    Not implemented. It is a dummy API to keep `sentry-cli upload-dif` happy
    """
    return None


def extract_proguard_id(name: str):
    match = re.search("proguard/([-a-fA-F0-9]+).txt", name)
    if match is None:
        return
    return match.group(1)


def extract_proguard_metadata(proguard_file):
    try:
        mapper = ProguardMapper.open(proguard_file)

        if mapper is None:
            return

        metadata = {"arch": "any", "feature": "mapping"}

        return metadata

    except Exception:
        pass


async def create_dif_from_read_only_file(proguard_file, project, proguard_id, filename):
    with tempfile.NamedTemporaryFile("br+") as tmp:
        content = proguard_file.read()
        tmp.write(content)
        tmp.flush()
        metadata = extract_proguard_metadata(tmp.name)
        if metadata is None:
            return None
        checksum = sha1(content).hexdigest()
        size = len(content)

        blob = await FileBlob.objects.filter(checksum=checksum).afirst()

        if blob is None:
            blob = FileBlob(checksum=checksum, size=size)  # noqa
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
                "debug_id": proguard_id,
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


@router.post("projects/{slug:organization_slug}/{slug:project_slug}/files/dsyms/")
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
