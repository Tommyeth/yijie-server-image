"""Import the immutable GHCR build into Modal and publish reusable Named Images."""

from __future__ import annotations

import modal


SOURCE_REVISION = "d935e07bd8b672a76eb742e2d40ea643ee24a6d1"
SOURCE_IMAGE = f"ghcr.io/tommyeth/yijie-server-image:{SOURCE_REVISION}"

BUILD_APP = "yijie-image-builds"
STABLE_NAME = "yijie-server-image:b11c768"
REVISION_NAME = f"yijie-server-image:{SOURCE_REVISION[:12]}"


def main() -> None:
    app = modal.App.lookup(BUILD_APP, create_if_missing=True)
    image = (
        modal.Image.from_registry(SOURCE_IMAGE, add_python="3.11")
        # The GHCR image starts its HTTP process via ENTRYPOINT. Modal Functions
        # and Sandboxes supply their own command, so publish a reusable base with
        # the entrypoint cleared. Start KataGo explicitly with
        # /opt/katago/bin/start.sh when a GPU container is created.
        .entrypoint([])
        .env(
            {
                "YIJIE_LISTEN_HOST": "127.0.0.1",
                "YIJIE_LISTEN_PORT": "2718",
                "YIJIE_MAX_CONCURRENT": "10",
                "YIJIE_MAX_SEARCH_SECONDS": "30",
                "YIJIE_DEFAULT_MAX_VISITS": "1000",
            }
        )
    )

    with modal.enable_output():
        built = image.build(app)
        built.publish(REVISION_NAME)
        built.publish(STABLE_NAME)

    print(f"source={SOURCE_IMAGE}")
    print(f"modal_image_id={built.object_id}")
    print(f"published={REVISION_NAME},{STABLE_NAME}")


if __name__ == "__main__":
    main()
