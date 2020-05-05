"""
Platform that will perform object detection.
"""
import io
import logging
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, UnidentifiedImageError

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.image_processing import (
    ATTR_CONFIDENCE,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SOURCE,
    PLATFORM_SCHEMA,
    ImageProcessingEntity,
)
from homeassistant.core import split_entity_id
from homeassistant.util.pil import draw_box

_LOGGER = logging.getLogger(__name__)

CONF_REGION = "region_name"
CONF_ACCESS_KEY_ID = "aws_access_key_id"
CONF_SECRET_ACCESS_KEY = "aws_secret_access_key"

DEFAULT_REGION = "us-east-1"
SUPPORTED_REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "ca-central-1",
    "eu-west-1",
    "eu-central-1",
    "eu-west-2",
    "eu-west-3",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-2",
    "ap-northeast-1",
    "ap-south-1",
    "sa-east-1",
]

CONF_SAVE_FILE_FOLDER = "save_file_folder"
CONF_TARGETS = "targets"
DEFAULT_TARGETS = ["person"]

CONF_SAVE_TIMESTAMPTED_FILE = "save_timestamped_file"
DATETIME_FORMAT = "%Y-%m-%d_%H:%M:%S"

CONF_BOTO_RETRIES = "boto_retries"
DEFAULT_BOTO_RETRIES = 5

REQUIREMENTS = ["boto3"]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): vol.In(SUPPORTED_REGIONS),
        vol.Required(CONF_ACCESS_KEY_ID): cv.string,
        vol.Required(CONF_SECRET_ACCESS_KEY): cv.string,
        vol.Optional(CONF_TARGETS, default=DEFAULT_TARGETS): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(CONF_SAVE_FILE_FOLDER): cv.isdir,
        vol.Optional(CONF_SAVE_TIMESTAMPTED_FILE, default=False): cv.boolean,
        vol.Optional(CONF_BOTO_RETRIES, default=DEFAULT_BOTO_RETRIES): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
    }
)


def get_object_instances(
    response: str, target: str, confidence_threshold: float
) -> int:
    """Get the number of instances of a target object above the confidence threshold."""
    for label in response["Labels"]:
        if (
            label["Name"].lower() == target.lower()
        ):  # Lowercase both to prevent any comparing issues
            confident_labels = [
                l for l in label["Instances"] if l["Confidence"] > confidence_threshold
            ]
            return confident_labels
    return []


def get_objects(response: str) -> dict:
    """Parse the data, returning detected objects only."""
    return {
        label["Name"].lower(): round(label["Confidence"], 1)
        for label in response["Labels"]
        if len(label["Instances"]) > 0
    }


def get_valid_filename(name: str) -> str:
    return re.sub(r"(?u)[^-\w.]", "", str(name).strip().replace(" ", "_"))


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up ObjectDetection."""

    import boto3

    _LOGGER.debug("boto_retries setting is {}".format(config[CONF_BOTO_RETRIES]))

    aws_config = {
        CONF_REGION: config[CONF_REGION],
        CONF_ACCESS_KEY_ID: config[CONF_ACCESS_KEY_ID],
        CONF_SECRET_ACCESS_KEY: config[CONF_SECRET_ACCESS_KEY],
    }

    retries = 0
    success = False
    while retries <= config[CONF_BOTO_RETRIES]:
        try:
            client = boto3.client("rekognition", **aws_config)
            success = True
            break
        except KeyError:
            _LOGGER.info("boto3 client failed, retries={}".format(retries))
            retries += 1
            time.sleep(1)

    if not success:
        raise Exception(
            "Failed to create boto3 client. Maybe try increasing "
            "the boto_retries setting. Retry counter was {}".format(retries)
        )

    save_file_folder = config.get(CONF_SAVE_FILE_FOLDER)
    if save_file_folder:
        save_file_folder = Path(save_file_folder)

    targets = [t.lower() for t in config[CONF_TARGETS]]  # ensure lower case

    entities = []
    for camera in config[CONF_SOURCE]:
        entities.append(
            ObjectDetection(
                client,
                config[CONF_REGION],
                targets,
                config[ATTR_CONFIDENCE],
                save_file_folder,
                config[CONF_SAVE_TIMESTAMPTED_FILE],
                camera[CONF_ENTITY_ID],
                camera.get(CONF_NAME),
            )
        )
    add_devices(entities)


class ObjectDetection(ImageProcessingEntity):
    """Perform object and label recognition."""

    def __init__(
        self,
        client,
        region,
        targets,
        confidence,
        save_file_folder,
        save_timestamped_file,
        camera_entity,
        name=None,
    ):
        """Init with the client."""
        self._client = client
        self._region = region
        self._targets = targets
        self._targets_found = [0] * len(self._targets)  #  for counting found targets
        self._confidence = confidence
        self._save_file_folder = save_file_folder
        self._save_timestamped_file = save_timestamped_file
        self._camera_entity = camera_entity
        if name:  # Since name is optional.
            self._name = name
        else:
            entity_name = split_entity_id(camera_entity)[1]
            self._name = f"rekognition_{entity_name}"
        self._state = None  # The number of instances of interest
        self._last_detection = None  # The last time we detected a target
        self._objects = {}  # The parsed label data

    def process_image(self, image):
        """Process an image."""
        self._state = None
        self._labels = {}
        self._targets_found = [0] * len(self._targets)

        response = self._client.detect_labels(Image={"Bytes": image})
        self._objects = get_objects(response)
        for i, target in enumerate(self._targets):
            self._targets_found[i] = len(
                get_object_instances(response, target, self._confidence)
            )

        self._state = sum(self._targets_found)

        if self._state > 0:
            self._last_detection = dt_util.now().strftime(DATETIME_FORMAT)

        if self._save_file_folder and self._state > 0:
            self.save_image(
                image,
                response,
                self._targets,
                self._confidence,
                self._save_file_folder,
                self._name,
            )

    @property
    def camera_entity(self):
        """Return camera entity id from process pictures."""
        return self._camera_entity

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        attr = self._objects
        attr[f"targets"] = self._targets
        if self._last_detection:
            attr[f"last_target_detection"] = self._last_detection
        return attr

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    def save_image(
        self, image, response, targets, confidence, directory, camera_entity
    ):
        """Draws the actual bounding box of the detected objects."""
        try:
            img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        except UnidentifiedImageError:
            _LOGGER.warning("Rekognition unable to process image, bad data")
            return
        draw = ImageDraw.Draw(img)

        for label in response["Labels"]:
            object_name = label["Name"].lower()
            if (label["Confidence"] < confidence) or (object_name not in targets):
                continue

            for instance in label["Instances"]:
                box = instance["BoundingBox"]

                x, y, w, h = box["Left"], box["Top"], box["Width"], box["Height"]
                x_max, y_max = x + w, y + h

                box_label = f'{object_name}: {label["Confidence"]:.1f}%'
                draw_box(
                    draw, (y, x, y_max, x_max), img.width, img.height, text=box_label,
                )

        latest_save_path = (
            directory / f"{get_valid_filename(self._name).lower()}_latest.jpg"
        )
        img.save(latest_save_path)

        if self._save_timestamped_file:
            timestamp_save_path = directory / f"{self._name}_{self._last_detection}.jpg"
            img.save(timestamp_save_path)
            _LOGGER.info("Rekognition saved file %s", timestamp_save_path)
