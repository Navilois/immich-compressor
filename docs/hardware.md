# Hardware encoding

The short version: **you do not have to configure this.** On every start the service
enumerates the render nodes it can see, reads the chip's vendor and device ids out of
sysfs, asks `ffmpeg -encoders` and `vainfo` what they support, and then confirms each
surviving candidate with a real one-frame encode. Whatever survives all of that is what it
uses.

```bash
docker compose exec immich-compressor immich-compressor hardware
```

That command prints the whole decision: the devices it found, the encoder it chose, **every
candidate it rejected and why**, the CPU budget it derived, the YAML to paste if you want to
pin the choice, and the calibration command for your machine. `--json` gives the same thing
in a form you can attach to a bug report.

## Why detection instead of a table

Because tables are wrong. Here is the same Intel UHD 630 (`0x8086:0x3e98`, Coffee Lake),
measured twice on the same machine:

| Where | `vainfo` HEVC entrypoints | `hevc_vaapi` one-frame encode |
|---|---|---|
| Host, with its own libva/driver install | `VAEntrypointVLD` only — decode | would fail |
| This project's image, with `intel-media-va-driver-non-free` | `VAEntrypointVLD`, `VAEntrypointEncSlice` | succeeds |

Same silicon, different answer, because the driver stack differs. A generation table cannot
know that. A one-frame encode inside the container that will actually do the work does.

The same run rejects `hevc_qsv` on that chip with the real error:

```
rejected  hevc_qsv on /dev/dri/renderD128
          the one-frame test encode failed: Error creating a MFX session: -9.
```

Debian trixie no longer ships `libmfx1`, and the bundled ffmpeg is built
`--disable-libmfx --enable-libvpl`, so it reaches the GPU through oneVPL only. oneVPL cannot
open a session on Gen9–11, and QSV is out. Older documentation for this project told you to
work that out yourself and edit a preset; now the service works it out in about a second.

## Support matrix

| Vendor | Arch | Encoder | How it is detected | Verified |
|---|---|---|---|---|
| Intel Gen12+ (Tiger Lake, Alder/Raptor Lake, N100, Arc) | amd64 | `hevc_qsv` | sysfs vendor `0x8086` + `ffmpeg -encoders` + one-frame encode | not on this project's hardware |
| Intel Gen9–11 (≤ 10th gen Core, UHD 630) | amd64 | `hevc_vaapi` | as above, after `hevc_qsv` fails its probe | **yes** — UHD 630, image driver stack |
| AMD (radeonsi) | amd64 | `hevc_vaapi` | sysfs vendor `0x1002` + `vainfo` HEVC encode entrypoint + one-frame encode | not on this project's hardware |
| NVIDIA | amd64 | `hevc_nvenc` | `/dev/nvidia*` or `nvidia-smi`, + one-frame encode | not on this project's hardware |
| Any | amd64 | `libx265` | always available, always last | **yes** |
| Any | arm64 | `libx265` | Intel packages do not exist for arm64; the image reports `this ffmpeg build has no hevc_qsv encoder` | **yes** — arm64 image under emulation |

"Not verified" means exactly that: the preset follows the vendor's documented setup and the
detection logic is unit-tested against captured tool output, but nobody has run it on that
silicon. `immich-compressor hardware` will tell you within seconds whether it works on
yours, because it runs a real encode before choosing. Reports are welcome — the
[hardware report issue template](https://github.com/Navilois/immich-compressor/issues/new?template=hardware_report.yml)
exists to turn them into a compatibility database.

## Wiring the GPU in

### Intel and AMD

```bash
RENDER_GID=$(getent group render | cut -d: -f3) \
  docker compose -f docker-compose.yaml -f docker-compose.gpu.yaml up -d
```

`immich-compressor setup` writes both `RENDER_GID` and
`COMPOSE_FILE=docker-compose.yaml:docker-compose.gpu.yaml` into `.env`, after which a plain
`docker compose up -d` picks the overlay up on its own. `docker-compose.override.yaml` is
created if it is missing and named on the same line: `COMPOSE_FILE` replaces compose's
default list, which is the only reason the override loads at all — see
[installation.md](installation.md).

The passthrough is a separate file on purpose: a `devices:` entry pointing at a `/dev/dri`
the host does not have makes the container **fail to start outright**, which is a much worse
failure than not having hardware encoding.

The render group is not optional either. The container runs as uid 10001 and cannot open
`renderD128` without it; the failure reads exactly like a broken driver. When that happens,
`immich-compressor hardware` says so in plain words and names the gid to add.

### NVIDIA

```bash
docker compose -f docker-compose.yaml -f docker-compose.gpu-nvidia.yaml up -d
```

Needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/)
on the host (`nvidia-ctk runtime configure`). There is no render group to join: the toolkit
injects the devices and the driver libraries itself.

## Pinning the choice

Detection runs on every start, which is what you want if the GPU is passed through
conditionally. If you would rather it never change, paste what `hardware` prints:

```yaml
hardware:
  mode: vaapi                       # auto | cpu | qsv | vaapi | nvenc
  render_node: /dev/dri/renderD128  # auto, or a specific node
```

A pinned mode is still a preference, not a promise: if the pinned encoder fails its
one-frame encode the service falls back to the CPU preset and logs why, rather than refusing
to start.

## Quality

`behavior.quality` maps to the right knob for whichever encoder was chosen, so you can tune
without knowing which of `-crf`, `-global_quality` and `-cq` applies:

| Encoder | Knob | `higher` | `balanced` | `smaller` |
|---|---|---|---|---|
| `libx265` | `-crf` | 23 | 26 | 29 |
| `hevc_qsv` | `-global_quality` (ICQ) | 23 | 26 | 30 |
| `hevc_vaapi` | `-global_quality` (ICQ) | 23 | 26 | 30 |
| `hevc_nvenc` | `-cq` | 25 | 28 | 32 |
| ImageMagick (stills) | `-quality` | 88 | 82 | 75 |

`balanced` is exactly what this project shipped and ran in 1.0.0, so upgrading changes
nothing you can see. These are starting points, not benchmarks.

## The stills preset

Stills are CPU-only on purpose. A GPU JPEG encoder exists but produces visibly worse output
at the same size than a competent software encoder, and a still is small enough that the
wall-clock saving is irrelevant. ImageMagick rather than `cjpegli`: Debian and Ubuntu
package `libjxl-tools` **without** the `cjpegli` binary — trixie's 0.11.2 ships only `cjxl`,
`djxl` and `jxlinfo`.

```
magick {input} -auto-orient -quality 82 -interlace Plane {output}
```

| Flag | Why |
|---|---|
| `magick`, not `convert` | `convert` is a deprecated alias in ImageMagick 7. |
| `-auto-orient` | Bakes the EXIF rotation into the pixels. Required by `normalize_orientation`, and validated at startup — the two belong together, see [architecture.md](architecture.md#rotation-and-orientation). |
| `-quality 82` | Measured on two sources: ratio 0.38 on a detail-rich 4000x3000 camera JPEG, 0.60 on a flat 3000x2000 one. |
| `-interlace Plane` | Progressive JPEG. Free: it reorders the same DCT coefficients, so the decoded pixels are bit-identical — verified with `compare -metric AE`, which returns **0** — while the file shrinks 3-8 %. |
| no `-sampling-factor` | ImageMagick then *inherits* the source's chroma subsampling (verified: a 4:4:4 source stays 4:4:4 even at q82). Forcing `4:2:0` would halve chroma resolution on every 4:4:4 source — visible on saturated edges, and invisible to every sanity check. |
| no `-strip` | The metadata copy restores tags afterwards, but keeping the ICC profile through the encode gives better colour. |

Four preset-level settings come with it, all of them consequences of a still being cheap to
encode and small to store:

| Setting | Value | Why |
|---|---|---|
| `match.extensions` | `.jpg .jpeg .jpe .jfif` | An allowlist, and the single most important line here — see [safety.md](safety.md#why-only-jpeg-stills). |
| `max_ratio` | `0.9` | Only a "something went badly wrong" net. On a cheap encode the ratio is the wrong axis: 0.75 on a 12 MB photo saves 3 MB, 0.60 on a 371 KB photo saves 147 KB. `min_savings_bytes` does the real work. |
| `min_source_quality` | `86` at `balanced` (92 / 79 at `higher` / `smaller`) | Four points above the preset's own quality target. Quantisation error is cumulative: a q60 source through the q82 preset was measured at 158 368 -> 190 488 bytes — a second generation of artefacts *and* a larger file. |
| `require_date_time_original` | `false` | A replacement's timeline position comes from `fileCreatedAt` at upload and the explicit `dateTimeOriginal` write afterwards, not from the file. |

**JPEG quality does not transfer across content.** At q82 a detail-rich 4000x3000 photo
lands at ratio 0.38 (SSIM 0.79), a flat 3000x2000 one at 0.60 (SSIM 0.98). A fixed SSIM
floor is therefore useless as a gate — any threshold that protects the second rejects the
first. Size ratio plus absolute bytes saved is what the gate uses instead. Check your own
material with `immich-compressor encode <file> --type IMAGE`.

ImageMagick is built with OpenMP and sizes its thread pool from the **host** core count,
ignoring the container's cgroup limit — the same trap the video preset defuses with
`pools=2 -threads 2`. The image sets `MAGICK_THREAD_LIMIT`, `MAGICK_MEMORY_LIMIT` and
`MAGICK_MAP_LIMIT` as environment variables so a hand-written preset is covered too. The
memory limits matter for large stills: the Q16 pixel cache is about 96 MB for a 12 MP image
but about 800 MB for a 100 MP panorama.

### Measuring instead of guessing

```bash
docker compose exec -e ENCODER=hevc_vaapi immich-compressor \
  scripts/calibrate.sh /path/inside/the/container/clip.mov
```

The script sweeps the quality knob over your own files and prints size ratio, SSIM and
encode time per setting. Take the **highest** quality number that still holds SSIM ≥ 0.98
and a ratio ≤ your `max_ratio`.

It also warns when the output size barely moves across the sweep. That matters on Intel: in
low-power (VDENC) mode some chips ignore ICQ entirely, and without the warning you would be
calibrating against a constant.

## Reading the numbers you get

`max_ratio: 0.6` is realistic for H.264 sources. Footage that is **already HEVC** — iPhone
11 and newer — will often fail the gate instead of shrinking, and will show up as
`skipped: no_gain`. That is the gate working.

Keep `concurrency: 1` on a GPU. An iGPU has one fixed-function encode block, and Immich's
own transcoding competes for the same `/dev/dri`. The service pins concurrency to 1 by
itself whenever a GPU preset is selected. It counts per worker lane, and there is one lane
per entry in `enabled_types` — with `[VIDEO, IMAGE]` that is one GPU video encode plus one
CPU still encode, which do not contend for the same silicon.

## The CPU budget

x265 sizes its thread pool from the **host** core count and ignores the container's cgroup
limit. An 8-thread pool inside a 2-core container starves everything else on the box,
Immich included.

The service reads `/sys/fs/cgroup/cpu.max` (falling back to `nproc`) and pins
`-x265-params pools=N -threads N` to the real budget. Measured on this project's own host:

```
$ docker run --rm --cpus 2 ... immich-compressor hardware
CPU budget:   2 effective core(s) from cgroup v2 cpu.max (host has 8)
              -> 2 encoder thread(s), concurrency 1
```

So sizing the container is the only thing you have to do — raise `cpus` in
`docker-compose.override.yaml` and the thread count follows. There is no second number to
keep in sync.

## The audio caveat

All three GPU presets copy the audio stream rather than re-encoding it — the point of the
exercise is the video, and a copy is both free and lossless. MP4 has no mapping for some of
what an old camera or a DVD rip produces, and ffmpeg's muxer refuses those files when it
writes the header. Measured on a live library on 2026-08-26, that was **119 of 172** failures
in one backfill run: `pcm_u8` (108), `amr_nb` (9) and `pcm_dvd` (2).

`behavior.transcode_unsupported_audio: true` re-encodes the audio to 128 kbit/s AAC on
exactly those jobs and leaves every other job copying as before — see
[troubleshooting.md](troubleshooting.md#videos-fail-with-could-not-find-tag-for-codec). The
CPU preset already encodes audio to AAC unconditionally, so it never meets this at all.

## The VAAPI caveat

The VAAPI preset does not carry `-map 0`: its filter chain does not survive extra streams,
so subtitle and data tracks are dropped. The QSV and CPU presets keep them. If your videos
carry subtitle tracks you care about, pin `hardware.mode: cpu`, or write your own preset —
see [configuration.md](configuration.md#presets).

## Troubleshooting

Start with `immich-compressor hardware`. It answers almost all of these directly.

| Symptom | Cause |
|---|---|
| `no DRM render node under /dev/dri` | The device was not passed through. Use `docker-compose.gpu.yaml`. |
| `cannot open /dev/dri/renderD128: permission denied` | Missing render group. The message names the gid to add. |
| `vainfo reports no HEVC encode entrypoint` | The driver in the container cannot encode HEVC on this chip. Not fixable from here; the CPU preset is used. |
| The same, but *"my GPU works fine for Immich"* | Immich's own transcoding targets H.264, which uses a different VA entrypoint. A chip can implement `VAProfileH264High : VAEntrypointEncSlice` and not `VAProfileHEVCMain : VAEntrypointEncSlice` — so a GPU that accelerates Immich can still be unable to encode HEVC. Compare the two lines in `vainfo` output. |
| `the one-frame test encode failed: Error creating a MFX session: -9` | Gen9–11 with QSV. Expected; `hevc_vaapi` is chosen instead. |
| `this ffmpeg build has no hevc_qsv encoder` | You are on arm64, where the Intel packages do not exist. |
| `no NVIDIA device found` | The NVIDIA Container Toolkit is not wired in. Use `docker-compose.gpu-nvidia.yaml`. |
| Plain `vainfo` fails with "can't connect to X server" | Missing `--display drm`. On a headless host that flag is mandatory; the service always passes it. |
