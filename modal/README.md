# Modal Named Image

`build_named_image.py` imports the immutable GHCR revision into Modal, caches
its layers, and publishes two reusable Named Images:

- `yijie-server-image:b11c768`
- `yijie-server-image:e29b26c12527`

The published Modal image clears the Docker `ENTRYPOINT` so it can be used by
Modal Functions and Sandboxes. Start the bundled KataGo HTTP node explicitly:

```python
image = modal.Image.from_name("yijie-server-image:b11c768")

sandbox = modal.Sandbox.create(
    "/opt/katago/bin/start.sh",
    image=image,
    app=app,
    gpu="L4",
)
```

The image itself does not keep a GPU container running. A GPU is billed only
when a Function or Sandbox using it is started.

To publish a newer GHCR build, update `SOURCE_REVISION`, then run:

```bash
python yijie-server-image/modal/build_named_image.py
```
