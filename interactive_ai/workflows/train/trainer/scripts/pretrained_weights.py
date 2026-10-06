# Copyright (C) 2022-2025 Intel Corporation
# LIMITED EDGE SOFTWARE DISTRIBUTION LICENSE

import json
import logging
import os
import traceback
import zipfile
from pathlib import Path

import requests
from minio.error import S3Error
from s3_client import S3ClientSingleton

logger = logging.getLogger("otx_job")

BUCKET_NAME_PRETRAINEDWEIGHTS = os.environ.get("BUCKET_NAME_PRETRAINEDWEIGHTS")
logger.info(f"PRETRAINEDWEIGHTS bucket name: {BUCKET_NAME_PRETRAINEDWEIGHTS}")


def download_file_from_url(object_name: str, file_path: str, manifest_url: str | None = None) -> None:
    """
    Download file from weights url and save it in target S3 bucket.
    Tries manifest_url first (public source), then falls back to WEIGHTS_URL.
    """
    urls_to_try = []
    if manifest_url:
        urls_to_try.append(manifest_url)
    urls_to_try.append(f"{os.environ.get('WEIGHTS_URL')}/{object_name}")

    last_exc: Exception | None = None
    for url in urls_to_try:
        try:
            resp = requests.get(url, timeout=600)
            if resp.status_code != 200:
                logger.warning(f"Got HTTP {resp.status_code} from {url}, trying next URL.")
                continue
            # When manifest URL is a zip but target is a pth, extract after download
            save_path = file_path
            if url.endswith(".zip") and not file_path.endswith(".zip"):
                save_path = file_path + ".zip"
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=512):
                    if chunk:
                        f.write(chunk)
            if save_path != file_path:
                with zipfile.ZipFile(save_path) as zip_ref:
                    zip_ref.extractall(os.path.dirname(file_path))
                os.remove(save_path)
            logger.info(f"File '{object_name}' downloaded successfully from {url} to '{file_path}'")
            S3ClientSingleton.instance().upload_file_from_local_disk(
                bucket_name=BUCKET_NAME_PRETRAINEDWEIGHTS,
                relative_path=object_name,
                local_file_path=file_path,
                overwrite=True,
            )
            logger.info(f"File '{object_name}' uploaded successfully to S3")
            return
        except Exception as exc:
            logger.warning(f"Failed to download '{object_name}' from {url}: {exc}")
            last_exc = exc

    logger.exception(f"Failed to download '{object_name}' from all URLs: {urls_to_try}")
    raise RuntimeError(f"Could not download '{object_name}'") from last_exc


def download_file(object_name, file_path, manifest_url: str | None = None):  # noqa: ANN001, ANN201, D103
    client = S3ClientSingleton.instance()
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    try:
        # Download the file from the S3 bucket
        client.download_file(bucket_name=BUCKET_NAME_PRETRAINEDWEIGHTS, relative_path=object_name, file_path=file_path)
    except S3Error as e:
        logger.error(f"Failed to download '{object_name}' from S3 to {file_path}: {e}.")
        if e.code == "NoSuchKey":
            download_file_from_url(object_name, file_path, manifest_url=manifest_url)
        else:
            logger.warning("Trying to get object using presigned URL")
            url = client.get_presigned_url(bucket_name=BUCKET_NAME_PRETRAINEDWEIGHTS, relative_path=object_name)
            try:
                resp = requests.get(url, timeout=600)
                if resp.status_code == 200:
                    with open(file_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=512):
                            if chunk:
                                f.write(chunk)
                    logger.info(f"File '{object_name}' downloaded successfully to '{file_path}' using presigned URL")
            except Exception:
                logger.exception(f"Failed to download '{object_name}' using presigned URL.")
                print(f"{traceback.print_exc()}")
                raise


def download_pretrained_weights(work_dir: Path, template_id: str) -> None:
    """Download pretrained weights from MinIO and save them to the given directory."""

    logger.info(f"downloading pretrained weights for template_id: {template_id}")

    # get model metadata file
    metadata_path = os.path.join(str(work_dir), "metadata.json")
    download_file("pretrained_models_v2.json", metadata_path)

    if not os.path.exists(metadata_path):
        raise RuntimeError(f"Metadata file {metadata_path} does not exist")

    with open(metadata_path) as f:
        metadata = json.load(f)
    # Determine obj_name depending on the config; collect (filename, manifest_url) pairs
    obj_names: list[tuple[str, str | None]] = []
    for model in metadata:
        template_ids = model.get("template_ids")
        if template_ids is not None and isinstance(template_ids, list):
            for id in template_ids:
                if id == template_id:
                    obj_names.append((os.path.basename(model["target"]), model.get("url")))
                    logger.info(
                        f"Found pretrained weights for template_id: {template_id},"
                        f" target: {os.path.basename(model['target'])}, url: {model.get('url')}"
                    )
                    break

    if len(obj_names) == 0:
        raise RuntimeError(f"Cannot find matched weights from model metadata for {template_id}")

    model_cache_dir = os.environ.get("MODEL_CACHE_DIR", "/home/non-root/.cache/torch/hub/checkpoints")
    for obj_name, manifest_url in obj_names:
        file_path = os.path.join(model_cache_dir, obj_name)
        download_file(obj_name, file_path, manifest_url=manifest_url)
        if file_path.endswith(".zip"):
            with zipfile.ZipFile(file_path) as zip_ref:
                zip_ref.extractall(os.path.dirname(file_path))
            os.remove(file_path)
        logger.info(f"Downloaded pretrained weights: {obj_name} to {file_path}")
