"""Builds small private .3mf packages (OPC/ZIP) for the Device Hub 3D tests; nothing is read from the machine."""
from __future__ import annotations

import zipfile
from pathlib import Path

CORE_2013 = "http://schemas.microsoft.com/3dmanufacturing/2013/01"
CORE_2015 = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MATERIAL_EXT = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_TEXTURE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dtexture"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '<Default Extension="png" ContentType="image/png"/></Types>'
)


def model_xml(namespace: str = CORE_2015, *, extensions: dict[str, str] | None = None,
              required: str = "") -> bytes:
    declarations = "".join(f' xmlns:{prefix}="{uri}"' for prefix, uri in (extensions or {}).items())
    required_attribute = f' requiredextensions="{required}"' if required else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{namespace}"{declarations}'
        f'{required_attribute}><resources><object id="1" type="model"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/>'
        '<vertex x="0" y="0" z="10"/></vertices><triangles><triangle v1="0" v2="2" v3="1"/>'
        '<triangle v1="0" v2="1" v3="3"/><triangle v1="0" v2="3" v3="2"/><triangle v1="1" v2="2" v3="3"/>'
        '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>'
    ).encode("utf-8")


def relationships(*items: tuple[str, str]) -> str:
    body = "".join(f'<Relationship Id="r{i}" Type="{kind}" Target="{target}"/>' for i, (kind, target) in
                   enumerate(items))
    return f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}</Relationships>'


def write_3mf(path: Path, *, model: bytes | None = None, textures: dict[str, bytes] | None = None,
              root_rels: str | None = None, extra: dict[str, bytes] | None = None) -> Path:
    """One package: [Content_Types].xml, _rels/.rels -> /3D/3dmodel.model, and optional textures."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", root_rels if root_rels is not None else
                         relationships((REL_MODEL, "/3D/3dmodel.model")))
        archive.writestr("3D/3dmodel.model", model if model is not None else model_xml())
        if textures:
            archive.writestr("3D/_rels/3dmodel.model.rels",
                             relationships(*((REL_TEXTURE, name) for name in textures)))
            for name, data in textures.items():
                archive.writestr(name.lstrip("/"), data)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return path
