"""Output templates ("moldes"): built-ins, the templates.yaml store and the ffmpeg compositor.

A Composition = a Template (where things go on the 1080x1920 canvas) + the source rects for
the camera and the gameplay (where things are in the stream, from the streamer calibration).
Shapes (rounded / circle) and borders are PNG masks drawn with Pillow and fed to ffmpeg as
looped image inputs, so the per-frame cost is a cheap alphamerge/overlay.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from PIL import Image, ImageDraw
from pydantic import ValidationError

from clipper.config import Layout, Rect, SlotSpec, StackLayout, Template
from clipper.media import MediaError

log = logging.getLogger(__name__)

OUT_W = 1080
OUT_H = 1920
FULL = Rect(x=0.0, y=0.0, w=1.0, h=1.0)
FALLBACK_ID = "blur-full"  # used when a template needs a camera but none was found


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


# ---- built-ins --------------------------------------------------------------


def _r(x: float, y: float, w: float, h: float) -> Rect:
    return Rect(x=x, y=y, w=w, h=h)


def _circle_w(h: float) -> float:
    """Width (output-normalized) that makes a slot of height `h` a square in pixels."""
    return h * OUT_H / OUT_W


BUILTIN_TEMPLATES: tuple[Template, ...] = (
    Template(
        id="stack-30", name="Câmera em cima · 30/70",
        slots=[SlotSpec(kind="cam", dst=_r(0, 0, 1, 0.30)), SlotSpec(kind="game", dst=_r(0, 0.30, 1, 0.70))],
        hook_y=0.30,
    ),
    Template(
        id="stack-50", name="Meio a meio · 50/50",
        slots=[SlotSpec(kind="cam", dst=_r(0, 0, 1, 0.5)), SlotSpec(kind="game", dst=_r(0, 0.5, 1, 0.5))],
        hook_y=0.50,
    ),
    Template(
        id="cam-bottom", name="Câmera embaixo · 70/30",
        slots=[SlotSpec(kind="game", dst=_r(0, 0, 1, 0.70)), SlotSpec(kind="cam", dst=_r(0, 0.70, 1, 0.30))],
        hook_y=0.12, caption_y=0.64,
    ),
    Template(
        id="pip-circle", name="Gameplay cheio · câmera em círculo",
        slots=[
            SlotSpec(kind="game", dst=FULL),
            SlotSpec(kind="cam", dst=_r(0.05, 0.05, round(_circle_w(0.21), 4), 0.21), shape="circle",
                     border_px=8, border_color="#22d3ee"),
        ],
        hook_y=0.36,
    ),
    Template(
        id="pip-rounded", name="Gameplay cheio · câmera arredondada",
        slots=[
            SlotSpec(kind="game", dst=FULL),
            SlotSpec(kind="cam", dst=_r(0.27, 0.05, 0.46, 0.15), shape="rounded", border_px=6),
        ],
        hook_y=0.28,
    ),
    Template(
        id=FALLBACK_ID, name="Só gameplay · fundo desfocado", background="blur",
        slots=[SlotSpec(kind="game", dst=FULL, fit="contain")],
        hook_y=0.31,
    ),
)  # fmt: skip
BUILTIN_IDS = frozenset(t.id for t in BUILTIN_TEMPLATES)


def legacy_template(layout: Layout) -> Template:
    """The template equivalent to a streamer's classic layout (no `template:` set)."""
    if isinstance(layout, StackLayout):
        r = layout.facecam_ratio
        return Template(
            id="legacy-stack", name=f"Câmera em cima · {round(r * 100)}%",
            slots=[SlotSpec(kind="cam", dst=_r(0, 0, 1, r)), SlotSpec(kind="game", dst=_r(0, r, 1, round(1 - r, 4)))],
            hook_y=r,
        )  # fmt: skip
    return builtin(FALLBACK_ID)


def builtin(template_id: str) -> Template:
    return next(t for t in BUILTIN_TEMPLATES if t.id == template_id)


# ---- store --------------------------------------------------------------------


class TemplateStore:
    """Built-ins (read-only) + user templates in a YAML file (machine-managed)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def custom(self) -> list[Template]:
        if not self.path.is_file():
            return []
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            return [Template.model_validate(t) for t in raw.get("templates", [])]
        except (yaml.YAMLError, ValidationError, AttributeError) as exc:
            log.error("invalid %s, ignoring custom templates: %s", self.path, exc)
            return []

    def all(self) -> list[Template]:
        return [*BUILTIN_TEMPLATES, *self.custom()]

    def get(self, template_id: str) -> Template | None:
        return next((t for t in self.all() if t.id == template_id), None)

    def save(self, template: Template) -> None:
        if template.id in BUILTIN_IDS:
            raise ValueError(f"'{template.id}' é um molde pronto; duplique para editar")
        items = [t for t in self.custom() if t.id != template.id] + [template]
        self._write(items)

    def delete(self, template_id: str) -> None:
        if template_id in BUILTIN_IDS:
            raise ValueError("moldes prontos não podem ser excluídos")
        items = self.custom()
        if not any(t.id == template_id for t in items):
            raise KeyError(template_id)
        self._write([t for t in items if t.id != template_id])

    def _write(self, items: list[Template]) -> None:
        header = "# Moldes criados no painel (gerenciado automaticamente; editar à mão também funciona).\n"
        body = yaml.safe_dump(
            {"templates": [t.model_dump(mode="json") for t in items]}, sort_keys=False, allow_unicode=True
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(header + body)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# ---- composition ------------------------------------------------------------------


@dataclass(frozen=True)
class Composition:
    template: Template
    cam_src: Rect | None   # where the camera is in the source (None: not found / no calibration)
    game_src: Rect = field(default_factory=lambda: FULL)

    def effective(self) -> Composition:
        """Falls back to the blur template when the template needs a camera we don't have."""
        if self.template.needs_cam and self.cam_src is None:
            return Composition(builtin(FALLBACK_ID), None, self.game_src)
        return self

    @property
    def hook_center_px(self) -> int:
        return int(round(self.template.hook_y * OUT_H))

    @property
    def caption_margin_px(self) -> int:
        return int(round((1 - self.template.caption_y) * OUT_H))


def composition_for(layout: Layout, template: Template | None, cam_src: Rect | None) -> Composition:
    game = layout.gameplay if isinstance(layout, StackLayout) else FULL
    tpl = template or legacy_template(layout)
    return Composition(tpl, cam_src, game).effective()


@dataclass(frozen=True)
class SlotAsset:
    filename: str
    kind: str  # "mask" | "border"
    slot_index: int


def _src_crop(r: Rect) -> str:
    return (
        f"crop=trunc(iw*{r.w}/2)*2:trunc(ih*{r.h}/2)*2:"
        f"trunc(iw*{r.x}/2)*2:trunc(ih*{r.y}/2)*2"
    )


def slot_box(slot: SlotSpec) -> tuple[int, int, int, int]:
    """Pixel box (x, y, w, h) of a slot on the output canvas; circles are forced square."""
    w, h = _even(slot.dst.w * OUT_W), _even(slot.dst.h * OUT_H)
    if slot.shape == "circle":
        w = h = min(w, h)
    x = min(int(round(slot.dst.x * OUT_W)), OUT_W - w)
    y = min(int(round(slot.dst.y * OUT_H)), OUT_H - h)
    return x, y, w, h


def slot_assets(comp: Composition) -> list[SlotAsset]:
    out: list[SlotAsset] = []
    for i, slot in enumerate(comp.template.slots):
        if slot.fit == "cover" and slot.shape != "rect":
            out.append(SlotAsset(f"slot{i}_mask.png", "mask", i))
        if slot.border_px > 0:
            out.append(SlotAsset(f"slot{i}_border.png", "border", i))
    return out


def write_slot_assets(comp: Composition, work_dir: Path) -> list[SlotAsset]:
    """Draws the mask/border PNGs for `comp` into work_dir (4x supersampled for smooth edges)."""
    assets = slot_assets(comp)
    work_dir.mkdir(parents=True, exist_ok=True)
    for a in assets:
        slot = comp.template.slots[a.slot_index]
        _, _, w, h = slot_box(slot)
        ss = 4
        big = (w * ss, h * ss)
        radius = (min(big) // 2) if slot.shape == "circle" else (int(min(big) * 0.12) if slot.shape == "rounded" else 0)

        def shape(draw: ImageDraw.ImageDraw, inset: int, fill: tuple[int, ...] | int, *, sl: SlotSpec = slot) -> None:
            box = (inset, inset, big[0] - 1 - inset, big[1] - 1 - inset)
            if sl.shape == "circle":
                draw.ellipse(box, fill=fill)
            else:
                draw.rounded_rectangle(box, radius=max(radius - inset, 0), fill=fill)

        if a.kind == "mask":
            img = Image.new("L", big, 0)
            shape(ImageDraw.Draw(img), 0, 255)
        else:
            color = tuple(int(slot.border_color[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
            img = Image.new("RGBA", big, (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            shape(d, 0, color)
            hole = Image.new("L", big, 0)
            shape(ImageDraw.Draw(hole), slot.border_px * ss, 255)
            img.putalpha(Image.composite(Image.new("L", big, 0), img.getchannel("A"), hole))
        img.resize((w, h), Image.Resampling.LANCZOS).save(work_dir / a.filename, format="PNG")
    return assets


def build_compose_graph(comp: Composition, first_asset_input: int) -> tuple[str, list[SlotAsset]]:
    """Filtergraph ending in [base] (captions/hook are appended by render.py).

    Input 0 is the clip; slot assets are looped PNG inputs starting at `first_asset_input`,
    in the order returned (render.py adds them with `-loop 1`).
    """
    tpl = comp.template
    assets = slot_assets(comp)
    input_of = {(a.slot_index, a.kind): first_asset_input + n for n, a in enumerate(assets)}
    n = len(tpl.slots) + 1
    parts = [f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n))]
    bg = f"[s{n - 1}]"
    if tpl.background == "blur":
        parts.append(
            f"{bg}scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"gblur=sigma=12,scale={OUT_W}:{OUT_H},eq=brightness=-0.08,setsar=1[l0]"
        )
    else:
        parts.append(f"{bg}scale={OUT_W}:{OUT_H},setsar=1,drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill[l0]")

    layer = "l0"
    for i, slot in enumerate(tpl.slots):
        src = comp.cam_src if slot.kind == "cam" else comp.game_src
        if src is None:  # effective() guarantees this can't happen for cam; be defensive
            raise MediaError(f"slot {i} needs a camera source")
        x, y, w, h = slot_box(slot)
        chain = f"[s{i}]{_src_crop(src)},"
        if slot.fit == "cover":
            chain += f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
            ox, oy = str(x), str(y)
        else:
            chain += f"scale={w}:{h}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
            ox, oy = f"{x}+({w}-w)/2", f"{y}+({h}-h)/2"
        if (i, "mask") in input_of:
            parts.append(f"{chain},format=rgba[c{i}]")
            parts.append(f"[{input_of[(i, 'mask')]}:v]format=gray[m{i}]")
            parts.append(f"[c{i}][m{i}]alphamerge=shortest=1[p{i}]")
        else:
            parts.append(f"{chain}[p{i}]")
        nxt = f"l{i + 1}"
        parts.append(f"[{layer}][p{i}]overlay=x={ox}:y={oy}:format=auto[{nxt}b]" if (i, "border") in input_of
                     else f"[{layer}][p{i}]overlay=x={ox}:y={oy}:format=auto[{nxt}]")  # fmt: skip
        if (i, "border") in input_of:
            parts.append(f"[{nxt}b][{input_of[(i, 'border')]}:v]overlay=x={x}:y={y}:format=auto:shortest=1[{nxt}]")
        layer = nxt
    parts.append(f"[{layer}]null[base]")
    return ";".join(parts), assets
