bl_info = {
    "name": "Rust Ripper",
    "author": "Rust Ripper",
    "version": (0, 2, 6),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Rust  |  File > Import",
    "description": "Import Rust Ripper GLB exports: PBR materials, blend layers, light tools, bridge connection",
    "category": "Import-Export",
}

import json
import os
import urllib.request

import bpy
from bpy.props import BoolProperty, FloatProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector

DAEMON = "http://127.0.0.1:17071"


# ---------------------------------------------------------------- settings

class RustRipperSettings(bpy.types.PropertyGroup):
    root_display_size: FloatProperty(
        name="Root Size", description="Display size of imported root empties",
        default=10.0, min=0.1, max=100.0)
    auto_hide: BoolProperty(
        name="Hide utility objects",
        description="Hide objects the game never shows (disabled renderers, IO origins, runtime flares)",
        default=True)
    reuse_meshes: BoolProperty(
        name="Reuse existing meshes",
        description="If a mesh with the same Unity identity is already in the file, link it instead of importing a duplicate (e.g. snow and normal pines share one mesh)",
        default=True)


# ---------------------------------------------------------------- core

def _mesh_key(mesh):
    pid = mesh.get("unity_path_id")
    return (str(pid), str(mesh.get("unity_collection"))) if pid is not None else None


def _post_process(objects, settings):
    """Apply everything the GLB carries but core glTF cannot express."""
    hidden = 0
    reused = 0
    new_meshes = {o.data for o in objects if o.type == "MESH"}
    registry = {}
    if settings.reuse_meshes:
        for mesh in bpy.data.meshes:
            key = _mesh_key(mesh)
            if key and mesh not in new_meshes and key not in registry:
                registry[key] = mesh
    for obj in objects:
        if settings.auto_hide and obj.get("unity_hidden"):
            obj.hide_set(True)
            obj.hide_render = True
            hidden += 1
        if obj.parent is None and obj.type == "EMPTY":
            obj.empty_display_size = settings.root_display_size
        if obj.type == "MESH" and settings.reuse_meshes:
            key = _mesh_key(obj.data)
            if key and key in registry and registry[key] is not obj.data:
                duplicate = obj.data
                obj.data = registry[key]
                if duplicate.users == 0:
                    bpy.data.meshes.remove(duplicate)
                reused += 1
            elif key:
                registry[key] = obj.data
        if obj.type == "LIGHT":
            info = _light_info(obj)
            if info:
                # glTF has no range: use Blender's custom distance cutoff so
                # small intense lights (gauges) stop flooding the scene
                rng = info.get("unity_range", 0)
                if rng and rng > 0:
                    obj.data.use_custom_distance = True
                    obj.data.cutoff_distance = rng
                if hasattr(obj.data, "shadow_soft_size"):
                    obj.data.shadow_soft_size = max(obj.data.shadow_soft_size, 0.03)
    return hidden, reused


def _build_paint_nodes(glb_path, materials):
    """Layer-nodes exports ship the albedo raw plus mask sidecars: build
    mask -> Mix.Factor, albedo -> A, _RUST_DETAILCOLOR attribute -> B."""
    base = os.path.splitext(glb_path)[0]
    built = 0
    for mat in materials:
        if not mat or not mat.use_nodes:
            continue
        mask_path = f"{base}.detailmask.{mat.name}.png"
        if not os.path.exists(mask_path):
            continue
        tree = mat.node_tree
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not bsdf or not bsdf.inputs["Base Color"].is_linked:
            continue
        albedo_out = bsdf.inputs["Base Color"].links[0].from_socket

        mask_img = _load_image(mask_path, srgb=False)
        mask_node = tree.nodes.new("ShaderNodeTexImage")
        mask_node.image = mask_img
        mask_node.label = "Paint Mask"
        mask_node.location = (-600, 500)

        attr = tree.nodes.new("ShaderNodeVertexColor")
        attr.layer_name = "_RUST_DETAILCOLOR"
        attr.label = "Detail Colour"
        attr.location = (-600, 250)

        mix = tree.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.label = "Paint"
        mix.location = (-300, 400)

        tree.links.new(mask_node.outputs["Color"], mix.inputs["Factor"])
        tree.links.new(albedo_out, mix.inputs[6])
        tree.links.new(attr.outputs["Color"], mix.inputs[7])
        tree.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
        built += 1
    return built


# ------------------------------------------------- blend layer (compiled-shader curve)

def _texture_entry(mat, slot):
    """unity_textures extras entry {name, scale, offset} for a slot, or None."""
    textures = mat.get("unity_textures")
    entry = textures.get(slot) if textures is not None and hasattr(textures, "get") else None
    return dict(entry) if entry is not None and hasattr(entry, "keys") else None


def _sidecar_path(glb_path, texture_name):
    path = f"{os.path.splitext(glb_path)[0]}.{texture_name}.png"
    return path if os.path.exists(path) else None


def _load_image(path, srgb):
    """Load keyed by (file, colour space). One texture can serve two roles
    with different spaces (a metal-gloss map reused as a blend mask): sharing
    one datablock lets the last loader flip the space for both, corrupting
    the other role. Same file + same space reuses; different space duplicates."""
    space = "sRGB" if srgb else "Non-Color"
    normalized = os.path.normpath(path)
    for img in bpy.data.images:
        if img.filepath and os.path.normpath(bpy.path.abspath(img.filepath)) == normalized \
                and img.colorspace_settings.name == space:
            return img
    img = bpy.data.images.load(path, check_existing=False)
    img.colorspace_settings.name = space
    return img


def _material_color_attributes(mat, objects):
    """Colour attribute names present on the imported meshes using this material."""
    names = set()
    for obj in objects:
        if obj.type == "MESH" and any(slot.material is mat for slot in obj.material_slots):
            names |= {a.name for a in obj.data.color_attributes}
    return names


def _uv_map_name(mat, objects, index):
    """The nth UV layer name on the meshes using this material (None = default)."""
    if index <= 0:
        return None
    for obj in objects:
        if obj.type == "MESH" and any(slot.material is mat for slot in obj.material_slots):
            layers = obj.data.uv_layers
            if len(layers) > index:
                return layers[index].name
    return None


def _wire_uv(tree, tex_node, mat, objects, uv_index, entry, label):
    """Feed a texture node the UV set the material data selects, with the
    slot's Unity tiling when authored (UVMap -> Mapping -> Vector)."""
    nodes, links = tree.nodes, tree.links
    scale = list(entry.get("scale", [1.0, 1.0])) if entry else [1.0, 1.0]
    offset = list(entry.get("offset", [0.0, 0.0])) if entry else [0.0, 0.0]
    uv_name = _uv_map_name(mat, objects, uv_index)
    tiled = scale != [1.0, 1.0] or offset != [0.0, 0.0]
    if not uv_name and not tiled:
        return
    source = None
    if uv_name or tiled:
        uv = nodes.new("ShaderNodeUVMap")
        if not uv_name:
            # UV set 0: name it explicitly instead of leaving the field empty
            for obj in objects:
                if obj.type == "MESH" and any(s.material is mat for s in obj.material_slots) and obj.data.uv_layers:
                    uv_name = obj.data.uv_layers[0].name
                    break
        if uv_name:
            uv.uv_map = uv_name
        uv.label = f"{label} UV{uv_index}"
        source = uv.outputs["UV"]
    if tiled:
        mapping = nodes.new("ShaderNodeMapping")
        mapping.label = f"{label} tiling (data)"
        mapping.inputs["Scale"].default_value = (scale[0], scale[1], 1.0)
        mapping.inputs["Location"].default_value = (offset[0], offset[1], 0.0)
        links.new(source, mapping.inputs["Vector"])
        source = mapping.outputs["Vector"]
    links.new(source, tex_node.inputs["Vector"])


_BLEND_LAYER_GROUP = "Rust/Standard Blend Layer"
_GROUP_VERSION = 5


def _blend_layer_group():
    """One shared node group per shader family - the Blender equivalent of
    the shader asset itself. Materials are instances: their own textures
    outside, their own authored values on the group's sliders. The math
    inside is the exact curve read from the game's compiled fragment
    programs (docs/OUTPUT_CONTRACT.md):

        blend = min(1, (vertexWeight * tintAlpha * mask.G * (_DetailBlendFactor + 1)) ** _DetailBlendFalloff)
        color = lerp(base, detailAlbedo * tint, blend)

    Versioned: an older group in the file gets missing sockets added and
    its internals rebuilt in place, so existing materials keep working.
    """
    group = bpy.data.node_groups.get(_BLEND_LAYER_GROUP)
    if group is not None and group.get("rust_ripper_version", 0) >= _GROUP_VERSION:
        return group
    if group is None:
        group = bpy.data.node_groups.new(_BLEND_LAYER_GROUP, "ShaderNodeTree")
    group["rust_ripper_version"] = _GROUP_VERSION
    group.use_fake_user = True

    present = {(item.name, item.in_out) for item in group.interface.items_tree
               if getattr(item, "in_out", None)}

    def socket(name, in_out, socket_type, default=None):
        if (name, in_out) in present:
            return
        item = group.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
        if default is not None:
            item.default_value = default

    socket("Base Color", "INPUT", "NodeSocketColor", (0.8, 0.8, 0.8, 1.0))
    socket("Detail Albedo", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Tint", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Tint Alpha", "INPUT", "NodeSocketFloat", 1.0)
    socket("Mask", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Vertex Weight", "INPUT", "NodeSocketFloat", 1.0)
    # shader defaults; each material instance sets its authored values
    socket("_DetailBlendFactor", "INPUT", "NodeSocketFloat", 8.0)
    socket("_DetailBlendFalloff", "INPUT", "NodeSocketFloat", 1.0)
    socket("_DetailBlendMaskMapInvert", "INPUT", "NodeSocketFloat", 0.0)
    socket("Base Metallic", "INPUT", "NodeSocketFloat", 0.0)
    socket("Base Roughness", "INPUT", "NodeSocketFloat", 1.0)
    socket("Layer Metallic", "INPUT", "NodeSocketFloat", 0.0)
    socket("Layer Roughness", "INPUT", "NodeSocketFloat", 1.0)
    socket("Base Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
    socket("Layer Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
    socket("Base Specular", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Layer Specular", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Color", "OUTPUT", "NodeSocketColor")
    socket("Metallic", "OUTPUT", "NodeSocketFloat")
    socket("Roughness", "OUTPUT", "NodeSocketFloat")
    socket("Normal", "OUTPUT", "NodeSocketVector")
    socket("Specular", "OUTPUT", "NodeSocketColor")
    socket("Blend Factor", "OUTPUT", "NodeSocketFloat")

    nodes, links = group.nodes, group.links
    nodes.clear()
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")

    def math(op, label, first=None, second=None):
        node = nodes.new("ShaderNodeMath")
        node.operation = op
        node.label = label
        if first is not None:
            node.inputs[0].default_value = first
        if second is not None:
            node.inputs[1].default_value = second
        return node

    sep = nodes.new("ShaderNodeSeparateColor")
    sep.label = "mask green (shader reads .g)"
    links.new(group_in.outputs["Mask"], sep.inputs["Color"])

    inverted = math("SUBTRACT", "1 - mask", first=1.0)
    links.new(sep.outputs["Green"], inverted.inputs[1])
    pick = nodes.new("ShaderNodeMix")
    pick.data_type = "FLOAT"
    pick.label = "_DetailBlendMaskMapInvert switch"
    links.new(group_in.outputs["_DetailBlendMaskMapInvert"], pick.inputs[0])
    links.new(sep.outputs["Green"], pick.inputs[2])
    links.new(inverted.outputs[0], pick.inputs[3])

    weighted = math("MULTIPLY", "mask x vertex weight")
    links.new(pick.outputs[0], weighted.inputs[0])
    links.new(group_in.outputs["Vertex Weight"], weighted.inputs[1])

    tint_alpha = math("MULTIPLY", "x tint alpha (weight = vcol.a x tint.a)")
    links.new(weighted.outputs[0], tint_alpha.inputs[0])
    links.new(group_in.outputs["Tint Alpha"], tint_alpha.inputs[1])

    gain = math("ADD", "_DetailBlendFactor + 1", second=1.0)
    links.new(group_in.outputs["_DetailBlendFactor"], gain.inputs[0])
    gained = math("MULTIPLY", "x (_DetailBlendFactor + 1)")
    links.new(tint_alpha.outputs[0], gained.inputs[0])
    links.new(gain.outputs[0], gained.inputs[1])

    curved = math("POWER", "^ _DetailBlendFalloff")
    links.new(gained.outputs[0], curved.inputs[0])
    links.new(group_in.outputs["_DetailBlendFalloff"], curved.inputs[1])
    clamped = math("MINIMUM", "min 1 (saturate)", second=1.0)
    links.new(curved.outputs[0], clamped.inputs[0])

    tinted = nodes.new("ShaderNodeMix")
    tinted.data_type = "RGBA"
    tinted.blend_type = "MULTIPLY"
    tinted.inputs[0].default_value = 1.0
    tinted.label = "detail layer (albedo x colour)"
    links.new(group_in.outputs["Detail Albedo"], tinted.inputs[6])
    links.new(group_in.outputs["Tint"], tinted.inputs[7])

    blend = nodes.new("ShaderNodeMix")
    blend.data_type = "RGBA"
    blend.blend_type = "MIX"
    blend.label = "blend by _DetailBlendMaskMap"
    links.new(clamped.outputs[0], blend.inputs[0])
    links.new(group_in.outputs["Base Color"], blend.inputs[6])
    links.new(tinted.outputs[2], blend.inputs[7])

    def float_lerp(label, a_name, b_name):
        node = nodes.new("ShaderNodeMix")
        node.data_type = "FLOAT"
        node.label = label
        links.new(clamped.outputs[0], node.inputs[0])
        links.new(group_in.outputs[a_name], node.inputs[2])
        links.new(group_in.outputs[b_name], node.inputs[3])
        return node

    metal_mix = float_lerp("metallic by blend", "Base Metallic", "Layer Metallic")
    rough_mix = float_lerp("roughness by blend", "Base Roughness", "Layer Roughness")

    # the compiled programs lerp unpacked normals by the same blend factor
    normal_mix = nodes.new("ShaderNodeMix")
    normal_mix.data_type = "VECTOR"
    normal_mix.label = "normal by blend"
    links.new(clamped.outputs[0], normal_mix.inputs[0])
    links.new(group_in.outputs["Base Normal"], normal_mix.inputs[4])
    links.new(group_in.outputs["Layer Normal"], normal_mix.inputs[5])
    normal_norm = nodes.new("ShaderNodeVectorMath")
    normal_norm.operation = "NORMALIZE"
    normal_norm.label = "renormalize"
    links.new(normal_mix.outputs[1], normal_norm.inputs[0])

    # specular workflow: layer F0 colour lerps with the same factor
    spec_mix = nodes.new("ShaderNodeMix")
    spec_mix.data_type = "RGBA"
    spec_mix.label = "specular by blend"
    links.new(clamped.outputs[0], spec_mix.inputs[0])
    links.new(group_in.outputs["Base Specular"], spec_mix.inputs[6])
    links.new(group_in.outputs["Layer Specular"], spec_mix.inputs[7])

    links.new(blend.outputs[2], group_out.inputs["Color"])
    links.new(metal_mix.outputs[0], group_out.inputs["Metallic"])
    links.new(rough_mix.outputs[0], group_out.inputs["Roughness"])
    links.new(normal_norm.outputs[0], group_out.inputs["Normal"])
    links.new(spec_mix.outputs[2], group_out.inputs["Specular"])
    links.new(clamped.outputs[0], group_out.inputs["Blend Factor"])
    _arrange_nodes(group)
    return group


def _wire_layer_metal_rough(tree, mat, objects, layer_node, glb_path, floats,
                            mg_slot, sg_slot, metallic_float, gloss_float, uv_index, label):
    """Feed a layer group's metal/rough inputs from the layer's own maps,
    using the exporter's established conversions: metal-gloss R=metal
    A=smoothness (roughness = 1 - A x glossiness), spec-gloss A=gloss
    (roughness only). Without a map, the material's own floats apply."""
    nodes, links = tree.nodes, tree.links
    mg_entry = _texture_entry(mat, mg_slot) if mg_slot else None
    sg_entry = _texture_entry(mat, sg_slot) if sg_slot else None
    entry = mg_entry or sg_entry
    path = entry and _sidecar_path(glb_path, entry["name"])
    gloss_scale = floats.get(gloss_float, 1.0) if gloss_float else 1.0
    if path:
        tex = nodes.new("ShaderNodeTexImage")
        # the sampler decodes sRGB-flagged textures' RGB (alpha stays linear
        # either way, so the gloss->roughness math is unaffected)
        tex.image = _load_image(path, entry.get("srgb", False))
        tex.label = f"{label} metal-gloss"
        _wire_uv(tree, tex, mat, objects, uv_index, entry, f"{label} mg")
        rough = nodes.new("ShaderNodeMath")
        rough.operation = "MULTIPLY_ADD"
        rough.label = f"1 - gloss x {gloss_float or 'scale'}"
        rough.inputs[1].default_value = -gloss_scale
        rough.inputs[2].default_value = 1.0
        links.new(tex.outputs["Alpha"], rough.inputs[0])
        links.new(rough.outputs[0], layer_node.inputs["Layer Roughness"])
        if mg_entry:
            sep = nodes.new("ShaderNodeSeparateColor")
            sep.label = f"{label} metal (R)"
            links.new(tex.outputs["Color"], sep.inputs["Color"])
            links.new(sep.outputs["Red"], layer_node.inputs["Layer Metallic"])
        else:
            # spec-gloss workflow: RGB is the layer's F0 colour - it lerps by
            # the same blend factor in the compiled programs
            links.new(tex.outputs["Color"], layer_node.inputs["Layer Specular"])
            layer_node.inputs["Layer Metallic"].default_value = floats.get(metallic_float, 0.0) if metallic_float else 0.0
    else:
        layer_node.inputs["Layer Metallic"].default_value = floats.get(metallic_float, 0.0) if metallic_float else 0.0
        layer_node.inputs["Layer Roughness"].default_value = 1.0 - gloss_scale


def _route_metal_rough_through(tree, bsdf, layer_node):
    """Re-route the BSDF's metallic/roughness through a layer group: current
    sources become the group's Base inputs, outputs drive the BSDF."""
    links = tree.links
    for bsdf_input, base_socket, out_socket in (
            (bsdf.inputs["Metallic"], "Base Metallic", "Metallic"),
            (bsdf.inputs["Roughness"], "Base Roughness", "Roughness")):
        if bsdf_input.is_linked:
            links.new(bsdf_input.links[0].from_socket, layer_node.inputs[base_socket])
        else:
            layer_node.inputs[base_socket].default_value = bsdf_input.default_value
        links.new(layer_node.outputs[out_socket], bsdf_input)


def _wire_layer_normal(tree, mat, objects, layer_node, glb_path, floats,
                       nrm_slot, scale_float, uv_index, label):
    """Feed a layer group's normal input from the layer's own normal map -
    the compiled programs unpack (x NormalMapScale), then lerp normals by
    the same factor as albedo. Mixing Normal Map node outputs (world space)
    and renormalizing is that same operation."""
    entry = _texture_entry(mat, nrm_slot)
    path = entry and _sidecar_path(glb_path, entry["name"])
    if not path:
        return
    nodes, links = tree.nodes, tree.links
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = _load_image(path, entry.get("srgb", False))
    tex.label = f"{label} normal"
    _wire_uv(tree, tex, mat, objects, uv_index, entry, f"{label} nrm")
    nmap = nodes.new("ShaderNodeNormalMap")
    nmap.label = f"{label} tangent normal"
    nmap.inputs["Strength"].default_value = floats.get(scale_float, 1.0) if scale_float else 1.0
    uv_name = _uv_map_name(mat, objects, uv_index)
    if uv_name:
        nmap.uv_map = uv_name
    links.new(tex.outputs["Color"], nmap.inputs["Color"])
    links.new(nmap.outputs["Normal"], layer_node.inputs["Layer Normal"])


def _route_specular_through(tree, bsdf, layer_node):
    """Chain the BSDF specular tint through a layer group when the layer
    brings its own spec-gloss map (specular workflow only)."""
    if not layer_node.inputs["Layer Specular"].is_linked:
        return
    links = tree.links
    spec_input = bsdf.inputs["Specular Tint"]
    if spec_input.is_linked:
        links.new(spec_input.links[0].from_socket, layer_node.inputs["Base Specular"])
    else:
        layer_node.inputs["Base Specular"].default_value = tuple(spec_input.default_value)
    links.new(layer_node.outputs["Specular"], spec_input)


def _route_normal_through(tree, bsdf, layer_node):
    """Chain the BSDF normal through a layer group when the layer brings its
    own normal map; without a base normal map the geometry normal stands in
    (a constant vector default would be wrong in world space)."""
    if not layer_node.inputs["Layer Normal"].is_linked:
        return
    links = tree.links
    normal_input = bsdf.inputs["Normal"]
    if normal_input.is_linked:
        links.new(normal_input.links[0].from_socket, layer_node.inputs["Base Normal"])
    else:
        geometry = next((n for n in tree.nodes if n.type == "NEW_GEOMETRY"), None)
        if geometry is None:
            geometry = tree.nodes.new("ShaderNodeNewGeometry")
            geometry.label = "geometry normal"
        links.new(geometry.outputs["Normal"], layer_node.inputs["Base Normal"])
    links.new(layer_node.outputs["Normal"], normal_input)


def _build_blend_layer_nodes(glb_path, materials, objects):
    """Materials with an active blend layer get a group instance wired to
    their own textures and attributes - samplers cannot pass through group
    sockets, so they live in the material and feed sampled colors in."""
    built = 0
    for mat in materials:
        if not mat or not mat.use_nodes:
            continue
        floats = mat.get("unity_floats")
        if floats is None or floats.get("_DetailBlendLayer", 0.0) != 1.0:
            continue
        mask_entry = _texture_entry(mat, "_DetailBlendMaskMap")
        detail_entry = _texture_entry(mat, "_DetailAlbedoMap")
        mask_path = mask_entry and _sidecar_path(glb_path, mask_entry["name"])
        detail_path = detail_entry and _sidecar_path(glb_path, detail_entry["name"])
        if not mask_path or not detail_path:
            continue
        tree = mat.node_tree
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        attrs = _material_color_attributes(mat, objects)
        nodes, links = tree.nodes, tree.links

        def image_node(path, label, entry, default_srgb):
            # the exporter records each texture's ColorSpace: Gamma-flagged
            # textures are sRGB-DECODED by the sampler before the shader sees
            # them - Blender must match or every mask threshold shifts
            srgb = entry.get("srgb", default_srgb) if entry else default_srgb
            node = nodes.new("ShaderNodeTexImage")
            node.image = _load_image(path, srgb)
            node.label = label
            return node

        layer = nodes.new("ShaderNodeGroup")
        layer.node_tree = _blend_layer_group()
        layer.label = _BLEND_LAYER_GROUP
        # authored values, straight from the material data
        layer.inputs["_DetailBlendFactor"].default_value = floats.get("_DetailBlendFactor", 8.0)
        layer.inputs["_DetailBlendFalloff"].default_value = floats.get("_DetailBlendFalloff", 1.0)
        layer.inputs["_DetailBlendMaskMapInvert"].default_value = floats.get("_DetailBlendMaskMapInvert", 0.0)

        mask_node = image_node(mask_path, "_DetailBlendMaskMap", mask_entry, default_srgb=False)
        if floats.get("_DetailBlendMaskAddLowFreq", 0.0) != 0.0:
            mask_node.label += " (AddLowFreq second sample not built)"
        _wire_uv(tree, mask_node, mat, objects,
                 int(floats.get("_DetailBlendMaskUVSet", 0.0)), mask_entry, "_DetailBlendMaskMap")
        links.new(mask_node.outputs["Color"], layer.inputs["Mask"])

        # meshes without a colour stream read (1,1,1,1) in Unity: leave weight at 1
        if "_RUST_COLOR" in attrs:
            vcol = nodes.new("ShaderNodeVertexColor")
            vcol.layer_name = "_RUST_COLOR"
            vcol.label = "vertex colour (blend weight)"
            if floats.get("_DetailBlendMaskVertexSource", 0.0) == 0.0:
                links.new(vcol.outputs["Alpha"], layer.inputs["Vertex Weight"])
            else:
                vsep = nodes.new("ShaderNodeSeparateColor")
                vsep.label = "_DetailBlendMaskVertexSource=1 (red)"
                links.new(vcol.outputs["Color"], vsep.inputs["Color"])
                links.new(vsep.outputs["Red"], layer.inputs["Vertex Weight"])

        detail_node = image_node(detail_path, "_DetailAlbedoMap", detail_entry, default_srgb=True)
        _wire_uv(tree, detail_node, mat, objects,
                 int(floats.get("_UVSec", 0.0)), detail_entry, "_DetailAlbedoMap")
        links.new(detail_node.outputs["Color"], layer.inputs["Detail Albedo"])

        # weight = vcol.a x tint.a in the compiled shader: alpha rides along
        if "_RUST_CUSTOMCOLOUR_01" in attrs:
            tint = nodes.new("ShaderNodeVertexColor")
            tint.layer_name = "_RUST_CUSTOMCOLOUR_01"
            tint.label = "customColour 01 (swap layer for other palette entries)"
            links.new(tint.outputs["Color"], layer.inputs["Tint"])
            links.new(tint.outputs["Alpha"], layer.inputs["Tint Alpha"])
        elif "_RUST_DETAILCOLOR" in attrs:
            tint = nodes.new("ShaderNodeVertexColor")
            tint.layer_name = "_RUST_DETAILCOLOR"
            tint.label = "_DetailColor (authored)"
            links.new(tint.outputs["Color"], layer.inputs["Tint"])
            links.new(tint.outputs["Alpha"], layer.inputs["Tint Alpha"])
        else:
            colors = mat.get("unity_colors")
            authored = list(colors.get("_DetailColor", [1.0, 1.0, 1.0, 1.0])) if colors is not None else [1.0, 1.0, 1.0, 1.0]
            layer.inputs["Tint"].default_value = (*authored[:3], 1.0)
            layer.inputs["Tint Alpha"].default_value = authored[3] if len(authored) > 3 else 1.0

        base_input = bsdf.inputs["Base Color"]
        if base_input.is_linked:
            links.new(base_input.links[0].from_socket, layer.inputs["Base Color"])
        else:
            layer.inputs["Base Color"].default_value = base_input.default_value
        links.new(layer.outputs["Color"], base_input)
        _wire_layer_metal_rough(tree, mat, objects, layer, glb_path, floats,
                                "_DetailMetallicGlossMap", None, "_DetailMetallic", "_DetailGlossiness",
                                int(floats.get("_UVSec", 0.0)), "_Detail")
        _route_metal_rough_through(tree, bsdf, layer)
        _wire_layer_normal(tree, mat, objects, layer, glb_path, floats,
                           "_DetailNormalMap", "_DetailNormalMapScale",
                           int(floats.get("_UVSec", 0.0)), "_Detail")
        _route_normal_through(tree, bsdf, layer)
        _route_specular_through(tree, bsdf, layer)
        built += 1
    return built


_BLEND4WAY_GROUP = "Rust/Standard Blend 4-Way (layer)"


def _blend4way_group():
    """One application of a numbered blend layer, chainable Color->Color.
    Curve read from the compiled 4-Way fragment programs - identical to the
    Blend Layer curve; weights come from vertex COLOR r/g/b per layer:

        blend = min(1, (weight * mask.G * (_BlendFactor + 1)) ** _BlendFalloff)
        layer = _AlbedoTintMask ? lerp(albedo, albedo * color, albedo.a)
                                : albedo * color
        out   = lerp(base, layer, blend)
    """
    group = bpy.data.node_groups.get(_BLEND4WAY_GROUP)
    if group is not None and group.get("rust_ripper_version", 0) >= 4:
        return group
    if group is None:
        group = bpy.data.node_groups.new(_BLEND4WAY_GROUP, "ShaderNodeTree")
    group["rust_ripper_version"] = 4
    group.use_fake_user = True

    present = {(item.name, item.in_out) for item in group.interface.items_tree
               if getattr(item, "in_out", None)}

    def socket(name, in_out, socket_type, default=None):
        if (name, in_out) in present:
            return
        item = group.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
        if default is not None:
            item.default_value = default

    socket("Base Color", "INPUT", "NodeSocketColor", (0.8, 0.8, 0.8, 1.0))
    socket("Layer Albedo", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Layer Albedo Alpha", "INPUT", "NodeSocketFloat", 1.0)
    socket("Layer Color", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("_AlbedoTintMask", "INPUT", "NodeSocketFloat", 0.0)
    socket("Mask", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Weight", "INPUT", "NodeSocketFloat", 1.0)
    socket("_BlendFactor", "INPUT", "NodeSocketFloat", 8.0)
    socket("_BlendFalloff", "INPUT", "NodeSocketFloat", 1.0)
    socket("_BlendMaskMapInvert", "INPUT", "NodeSocketFloat", 0.0)
    socket("Base Metallic", "INPUT", "NodeSocketFloat", 0.0)
    socket("Base Roughness", "INPUT", "NodeSocketFloat", 1.0)
    socket("Layer Metallic", "INPUT", "NodeSocketFloat", 0.0)
    socket("Layer Roughness", "INPUT", "NodeSocketFloat", 1.0)
    socket("Base Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
    socket("Layer Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
    socket("Base Specular", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Layer Specular", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    socket("Color", "OUTPUT", "NodeSocketColor")
    socket("Metallic", "OUTPUT", "NodeSocketFloat")
    socket("Roughness", "OUTPUT", "NodeSocketFloat")
    socket("Normal", "OUTPUT", "NodeSocketVector")
    socket("Specular", "OUTPUT", "NodeSocketColor")
    socket("Blend Factor", "OUTPUT", "NodeSocketFloat")

    nodes, links = group.nodes, group.links
    nodes.clear()
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")

    def math(op, label, first=None, second=None):
        node = nodes.new("ShaderNodeMath")
        node.operation = op
        node.label = label
        if first is not None:
            node.inputs[0].default_value = first
        if second is not None:
            node.inputs[1].default_value = second
        return node

    def mix_rgba(blend_type, label):
        node = nodes.new("ShaderNodeMix")
        node.data_type = "RGBA"
        node.blend_type = blend_type
        node.label = label
        return node

    sep = nodes.new("ShaderNodeSeparateColor")
    sep.label = "mask green (shader reads .g)"
    links.new(group_in.outputs["Mask"], sep.inputs["Color"])

    inverted = math("SUBTRACT", "1 - mask", first=1.0)
    links.new(sep.outputs["Green"], inverted.inputs[1])
    pick = nodes.new("ShaderNodeMix")
    pick.data_type = "FLOAT"
    pick.label = "_BlendMaskMapInvert switch"
    links.new(group_in.outputs["_BlendMaskMapInvert"], pick.inputs[0])
    links.new(sep.outputs["Green"], pick.inputs[2])
    links.new(inverted.outputs[0], pick.inputs[3])

    weighted = math("MULTIPLY", "mask x vertex weight")
    links.new(pick.outputs[0], weighted.inputs[0])
    links.new(group_in.outputs["Weight"], weighted.inputs[1])

    gain = math("ADD", "_BlendFactor + 1", second=1.0)
    links.new(group_in.outputs["_BlendFactor"], gain.inputs[0])
    gained = math("MULTIPLY", "x (_BlendFactor + 1)")
    links.new(weighted.outputs[0], gained.inputs[0])
    links.new(gain.outputs[0], gained.inputs[1])
    curved = math("POWER", "^ _BlendFalloff")
    links.new(gained.outputs[0], curved.inputs[0])
    links.new(group_in.outputs["_BlendFalloff"], curved.inputs[1])
    clamped = math("MINIMUM", "min 1 (saturate)", second=1.0)
    links.new(curved.outputs[0], clamped.inputs[0])

    tint_full = mix_rgba("MULTIPLY", "albedo x _Color")
    tint_full.inputs[0].default_value = 1.0
    links.new(group_in.outputs["Layer Albedo"], tint_full.inputs[6])
    links.new(group_in.outputs["Layer Color"], tint_full.inputs[7])

    tint_masked = mix_rgba("MIX", "tint through albedo alpha")
    links.new(group_in.outputs["Layer Albedo Alpha"], tint_masked.inputs[0])
    links.new(group_in.outputs["Layer Albedo"], tint_masked.inputs[6])
    links.new(tint_full.outputs[2], tint_masked.inputs[7])

    tint_pick = mix_rgba("MIX", "_AlbedoTintMask switch")
    links.new(group_in.outputs["_AlbedoTintMask"], tint_pick.inputs[0])
    links.new(tint_full.outputs[2], tint_pick.inputs[6])
    links.new(tint_masked.outputs[2], tint_pick.inputs[7])

    blend = mix_rgba("MIX", "blend layer")
    links.new(clamped.outputs[0], blend.inputs[0])
    links.new(group_in.outputs["Base Color"], blend.inputs[6])
    links.new(tint_pick.outputs[2], blend.inputs[7])

    def float_lerp(label, a_name, b_name):
        node = nodes.new("ShaderNodeMix")
        node.data_type = "FLOAT"
        node.label = label
        links.new(clamped.outputs[0], node.inputs[0])
        links.new(group_in.outputs[a_name], node.inputs[2])
        links.new(group_in.outputs[b_name], node.inputs[3])
        return node

    metal_mix = float_lerp("metallic by blend", "Base Metallic", "Layer Metallic")
    rough_mix = float_lerp("roughness by blend", "Base Roughness", "Layer Roughness")

    # the compiled programs lerp unpacked normals by the same blend factor
    normal_mix = nodes.new("ShaderNodeMix")
    normal_mix.data_type = "VECTOR"
    normal_mix.label = "normal by blend"
    links.new(clamped.outputs[0], normal_mix.inputs[0])
    links.new(group_in.outputs["Base Normal"], normal_mix.inputs[4])
    links.new(group_in.outputs["Layer Normal"], normal_mix.inputs[5])
    normal_norm = nodes.new("ShaderNodeVectorMath")
    normal_norm.operation = "NORMALIZE"
    normal_norm.label = "renormalize"
    links.new(normal_mix.outputs[1], normal_norm.inputs[0])

    # specular workflow: layer F0 colour lerps with the same factor
    spec_mix = nodes.new("ShaderNodeMix")
    spec_mix.data_type = "RGBA"
    spec_mix.label = "specular by blend"
    links.new(clamped.outputs[0], spec_mix.inputs[0])
    links.new(group_in.outputs["Base Specular"], spec_mix.inputs[6])
    links.new(group_in.outputs["Layer Specular"], spec_mix.inputs[7])

    links.new(blend.outputs[2], group_out.inputs["Color"])
    links.new(metal_mix.outputs[0], group_out.inputs["Metallic"])
    links.new(rough_mix.outputs[0], group_out.inputs["Roughness"])
    links.new(normal_norm.outputs[0], group_out.inputs["Normal"])
    links.new(spec_mix.outputs[2], group_out.inputs["Specular"])
    links.new(clamped.outputs[0], group_out.inputs["Blend Factor"])
    _arrange_nodes(group)
    return group


def _build_blend4way_nodes(glb_path, materials, objects):
    """Numbered blend layers (_BlendLayer1..3): chain one group instance per
    enabled layer; weights are vertex COLOR r/g/b, all values from data."""
    built = 0
    for mat in materials:
        if not mat or not mat.use_nodes:
            continue
        floats = mat.get("unity_floats")
        if floats is None:
            continue
        layers = []
        for n in (1, 2, 3):
            if floats.get(f"_BlendLayer{n}", 0.0) != 1.0:
                continue
            albedo_entry = _texture_entry(mat, f"_BlendLayer{n}_AlbedoMap")
            mask_entry = _texture_entry(mat, f"_BlendLayer{n}_BlendMaskMap")
            albedo_path = albedo_entry and _sidecar_path(glb_path, albedo_entry["name"])
            mask_path = mask_entry and _sidecar_path(glb_path, mask_entry["name"])
            if albedo_path and mask_path:
                layers.append((n, albedo_entry, albedo_path, mask_entry, mask_path))
        if not layers:
            continue
        tree = mat.node_tree
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        attrs = _material_color_attributes(mat, objects)
        nodes, links = tree.nodes, tree.links
        colors = mat.get("unity_colors")

        weight_sep = None
        if "_RUST_COLOR" in attrs:
            vcol = nodes.new("ShaderNodeVertexColor")
            vcol.layer_name = "_RUST_COLOR"
            vcol.label = "vertex colour (layer weights r/g/b)"
            weight_sep = nodes.new("ShaderNodeSeparateColor")
            weight_sep.label = "layer weights"
            links.new(vcol.outputs["Color"], weight_sep.inputs["Color"])

        base_input = bsdf.inputs["Base Color"]
        chain_socket = base_input.links[0].from_socket if base_input.is_linked else None
        for n, albedo_entry, albedo_path, mask_entry, mask_path in layers:
            layer = nodes.new("ShaderNodeGroup")
            layer.node_tree = _blend4way_group()
            layer.label = f"_BlendLayer{n}"
            layer.inputs["_BlendFactor"].default_value = floats.get(f"_BlendLayer{n}_BlendFactor", 8.0)
            layer.inputs["_BlendFalloff"].default_value = floats.get(f"_BlendLayer{n}_BlendFalloff", 1.0)
            layer.inputs["_BlendMaskMapInvert"].default_value = floats.get(f"_BlendLayer{n}_BlendMaskMapInvert", 0.0)
            layer.inputs["_AlbedoTintMask"].default_value = floats.get(f"_BlendLayer{n}_AlbedoTintMask", 0.0)
            if colors is not None:
                authored = list(colors.get(f"_BlendLayer{n}_Color", [1.0, 1.0, 1.0, 1.0]))
                layer.inputs["Layer Color"].default_value = (*[c ** 2.2 for c in authored[:3]], 1.0)

            # sampler colour space comes from the texture asset (extras srgb)
            albedo_node = nodes.new("ShaderNodeTexImage")
            albedo_node.image = _load_image(albedo_path, albedo_entry.get("srgb", True))
            albedo_node.label = f"_BlendLayer{n}_AlbedoMap"
            _wire_uv(tree, albedo_node, mat, objects,
                     int(floats.get(f"_BlendLayer{n}_UVSet", 0.0)), albedo_entry, f"_BlendLayer{n}")
            links.new(albedo_node.outputs["Color"], layer.inputs["Layer Albedo"])
            links.new(albedo_node.outputs["Alpha"], layer.inputs["Layer Albedo Alpha"])

            mask_node = nodes.new("ShaderNodeTexImage")
            mask_node.image = _load_image(mask_path, mask_entry.get("srgb", False))
            mask_node.label = f"_BlendLayer{n}_BlendMaskMap"
            _wire_uv(tree, mask_node, mat, objects,
                     int(floats.get(f"_BlendLayer{n}_BlendMaskUVSet", 0.0)), mask_entry, f"_BlendLayer{n} mask")
            links.new(mask_node.outputs["Color"], layer.inputs["Mask"])

            if weight_sep is not None:
                links.new(weight_sep.outputs[("Red", "Green", "Blue")[n - 1]], layer.inputs["Weight"])

            if chain_socket is not None:
                links.new(chain_socket, layer.inputs["Base Color"])
            else:
                layer.inputs["Base Color"].default_value = base_input.default_value
            chain_socket = layer.outputs["Color"]
            # metal/rough chain: routing through each layer in order makes
            # the previous layer's output the next one's base automatically
            _wire_layer_metal_rough(tree, mat, objects, layer, glb_path, floats,
                                    f"_BlendLayer{n}_MetallicGlossMap", f"_BlendLayer{n}_SpecGlossMap",
                                    f"_BlendLayer{n}_Metallic", f"_BlendLayer{n}_Glossiness",
                                    int(floats.get(f"_BlendLayer{n}_UVSet", 0.0)), f"_BlendLayer{n}")
            _route_metal_rough_through(tree, bsdf, layer)
            _wire_layer_normal(tree, mat, objects, layer, glb_path, floats,
                               f"_BlendLayer{n}_NormalMap", f"_BlendLayer{n}_NormalMapScale",
                               int(floats.get(f"_BlendLayer{n}_UVSet", 0.0)), f"_BlendLayer{n}")
            _route_normal_through(tree, bsdf, layer)
            _route_specular_through(tree, bsdf, layer)
        links.new(chain_socket, base_input)
        built += 1
    return built


_ALPHA_CLIP_GROUP = "Rust Alpha Clip"


def _alpha_clip_group():
    """One tiny shared group for alpha testing: Alpha >= _Cutoff -> 1 else 0.
    Replaces the importer's sprawling Alpha Clip frame with a single node."""
    group = bpy.data.node_groups.get(_ALPHA_CLIP_GROUP)
    if group is not None and group.get("rust_ripper_version", 0) >= 1:
        return group
    if group is None:
        group = bpy.data.node_groups.new(_ALPHA_CLIP_GROUP, "ShaderNodeTree")
    group["rust_ripper_version"] = 1
    group.use_fake_user = True
    present = {(item.name, item.in_out) for item in group.interface.items_tree
               if getattr(item, "in_out", None)}

    def socket(name, in_out, socket_type, default=None):
        if (name, in_out) in present:
            return
        item = group.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
        if default is not None:
            item.default_value = default

    socket("Alpha", "INPUT", "NodeSocketFloat", 1.0)
    socket("_Cutoff", "INPUT", "NodeSocketFloat", 0.5)
    socket("Alpha", "OUTPUT", "NodeSocketFloat")

    nodes, links = group.nodes, group.links
    nodes.clear()
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    cut = nodes.new("ShaderNodeMath")
    cut.operation = "GREATER_THAN"
    cut.label = "alpha >= _Cutoff"
    links.new(group_in.outputs["Alpha"], cut.inputs[0])
    links.new(group_in.outputs["_Cutoff"], cut.inputs[1])
    links.new(cut.outputs[0], group_out.inputs["Alpha"])
    _arrange_nodes(group)
    return group


def _compact_alpha_clips(materials):
    """Swap the glTF importer's Alpha Clip frame (two chained math nodes) for
    the shared Rust Alpha Clip group; cutoff comes from the material data."""
    built = 0
    for mat in materials:
        if not mat or not mat.use_nodes:
            continue
        tree = mat.node_tree
        frame = next((n for n in tree.nodes if n.type == "FRAME" and n.label == "Alpha Clip"), None)
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if frame is None or bsdf is None or not bsdf.inputs["Alpha"].is_linked:
            continue
        members = [n for n in tree.nodes if n.parent is frame]
        # the chain's source: whatever feeds a member from outside the frame
        source = None
        cutoff = None
        for n in members:
            for sock in n.inputs:
                for link in sock.links:
                    if link.from_node not in members:
                        source = link.from_socket
            for sock in n.inputs:
                if not sock.is_linked and sock.type == "VALUE" and sock.default_value not in (0.0, 1.0):
                    cutoff = sock.default_value
        if source is None:
            continue
        floats = mat.get("unity_floats")
        if floats is not None and "_Cutoff" in floats.keys():
            cutoff = floats["_Cutoff"]
        clip = tree.nodes.new("ShaderNodeGroup")
        clip.node_tree = _alpha_clip_group()
        clip.label = _ALPHA_CLIP_GROUP
        if cutoff is not None:
            clip.inputs["_Cutoff"].default_value = cutoff
        tree.links.new(source, clip.inputs["Alpha"])
        tree.links.new(clip.outputs["Alpha"], bsdf.inputs["Alpha"])
        for n in members:
            tree.nodes.remove(n)
        tree.nodes.remove(frame)
        built += 1
    return built


def _count_fur_materials(materials):
    """Alpha-tested fur (AnimalFur): the glTF MASK import (raw albedo alpha,
    alphaCutoff = _Cutoff) is the right graph as-is - the full shader formula
    additionally lerps toward vertex red (docs/OUTPUT_CONTRACT.md) but the
    plain clip reads correctly. Detected by the mechanism's own parameters,
    only to raise the Cycles transparent bounce budget for the shell stack."""
    count = 0
    for mat in materials:
        if not mat or not mat.use_nodes:
            continue
        floats = mat.get("unity_floats")
        if floats is not None and all(k in floats.keys() for k in ("_AlphaLerp", "_AlphaNudge", "_Cutoff")):
            count += 1
    return count


_DECAL_SHADERS = {
    "Rust/Standard Decal",
    "Rust/Standard Decal (Specular setup)",
    "Rust/Standard Decal (Poster)",
    "Decal/Deferred Decal",
}


def _build_decal_clips(materials):
    """Decal-family materials render through the deferred decal system,
    whose compiled program DISCARDS below _Cutoff before blending. Plain
    glTF BLEND shows the sub-cutoff content the game clips - the authored
    soft drop shadows in the atlas alpha. Rebuild the game's behaviour:
    alpha' = alpha x (alpha >= _Cutoff), material stays BLEND."""
    built = 0
    for mat in materials:
        if not mat or not mat.use_nodes:
            continue
        if mat.get("unity_shader", "") not in _DECAL_SHADERS:
            continue
        floats = mat.get("unity_floats")
        cutoff = floats.get("_Cutoff", 0.0) if floats is not None else 0.0
        if cutoff <= 0.0 or mat.surface_render_method != "BLENDED":
            continue
        tree = mat.node_tree
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None or not bsdf.inputs["Alpha"].is_linked:
            continue
        alpha_src = bsdf.inputs["Alpha"].links[0].from_socket
        gate = tree.nodes.new("ShaderNodeMath")
        gate.operation = "GREATER_THAN"
        gate.label = "decal discard at _Cutoff"
        gate.inputs[1].default_value = cutoff
        clipped = tree.nodes.new("ShaderNodeMath")
        clipped.operation = "MULTIPLY"
        clipped.label = "clip, then blend"
        tree.links.new(alpha_src, gate.inputs[0])
        tree.links.new(alpha_src, clipped.inputs[0])
        tree.links.new(gate.outputs[0], clipped.inputs[1])
        tree.links.new(clipped.outputs[0], bsdf.inputs["Alpha"])
        built += 1
    return built


# ------------------------------------------------------------- node cleanup

def _ensure_weight_attributes(objects, materials):
    """Unity substitutes WHITE for a missing vertex-colour stream, so layer
    weights on colourless meshes read 1.0 in game. EEVEE instead renders a
    missing attribute reference as error pink - give those meshes a white
    _RUST_COLOR so the graphs shade exactly like the game."""
    referencing = {m.name for m in materials if m and m.use_nodes and any(
        n.type == "VERTEX_COLOR" and n.layer_name == "_RUST_COLOR" for n in m.node_tree.nodes)}
    added = 0
    for obj in objects:
        if obj.type != "MESH" or "_RUST_COLOR" in obj.data.color_attributes:
            continue
        if any(s.material and s.material.name in referencing for s in obj.material_slots):
            attr = obj.data.color_attributes.new("_RUST_COLOR", "BYTE_COLOR", "POINT")
            attr.data.foreach_set("color", [1.0] * (len(attr.data) * 4))
            added += 1
    return added


def _prune_unused_nodes(tree):
    """Remove nodes with no path to any output (Material Output or the glTF
    settings group): dangling UV maps, orphaned images, importer leftovers.
    Unreachable nodes cannot affect shading, so this is always safe."""
    sinks = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"
             or (n.type == "GROUP" and n.node_tree and n.node_tree.name.startswith("glTF"))]
    keep = set(sinks)
    stack = list(sinks)
    while stack:
        node = stack.pop()
        for socket in node.inputs:
            for link in socket.links:
                if link.from_node not in keep:
                    keep.add(link.from_node)
                    stack.append(link.from_node)
    removed = 0
    for node in list(tree.nodes):
        if node not in keep and node.type != "FRAME":
            tree.nodes.remove(node)
            removed += 1
    return removed


# ------------------------------------------------------------- node layout
#
# Self-contained layered graph layout (the Sugiyama method):
#   1. rank   - longest link distance to the output side = column
#   2. split  - edges spanning several columns get virtual waypoints so the
#               ordering step can route them between rows instead of across
#   3. order  - iterative barycenter sweeps minimize link crossings
#   4. place  - socket-anchored positions relaxed under no-overlap
#               constraints; column x from real node widths

_NODE_HEIGHT = {
    "TEX_IMAGE": 290, "BSDF_PRINCIPLED": 640, "MIX": 190, "MATH": 160,
    "VALUE": 90, "RGB": 200, "SEPARATE_COLOR": 130, "NORMAL_MAP": 170,
    "MAPPING": 330, "UVMAP": 100, "VERTEX_COLOR": 120, "GROUP": 300,
    "GROUP_INPUT": 300, "GROUP_OUTPUT": 120,
    "OUTPUT_MATERIAL": 110,
}

_MARGIN_X = 60.0
_MARGIN_Y = 40.0


class _Waypoint:
    """Virtual node standing in for a long edge crossing a column."""
    __slots__ = ("rank",)

    def __init__(self, rank):
        self.rank = rank


def _socket_anchor(index):
    """Approximate y offset of a socket from the node's top edge."""
    return -(35.0 + 22.0 * index)


def _arrange_nodes(tree):
    nodes = [n for n in tree.nodes if n.type != "FRAME"]
    if not nodes:
        return
    node_set = set(nodes)
    links = [l for l in tree.links if l.from_node in node_set and l.to_node in node_set]

    # 1. rank: longest path to a sink (rank 0 = output column, rightmost)
    rank = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for l in links:
            if rank[l.from_node] < rank[l.to_node] + 1:
                rank[l.from_node] = rank[l.to_node] + 1
                changed = True
        if not changed:
            break

    def node_height(n):
        if isinstance(n, _Waypoint):
            return 20.0
        dims = getattr(n, "dimensions", None)
        if dims is not None and dims.y > 1.0:
            return float(dims.y)
        return float(_NODE_HEIGHT.get(n.type, 160))

    def node_width(n):
        return 0.0 if isinstance(n, _Waypoint) else float(getattr(n, "width", 140.0))

    # 2. split long edges with waypoints
    edge_list = []
    waypoints = []
    for l in links:
        a, b = l.from_node, l.to_node
        try:
            out_idx = list(a.outputs).index(l.from_socket)
            in_idx = list(b.inputs).index(l.to_socket)
        except ValueError:
            out_idx = in_idx = 0
        chain = [a]
        for r in range(rank[a] - 1, rank[b], -1):
            waypoint = _Waypoint(r)
            waypoints.append(waypoint)
            chain.append(waypoint)
        chain.append(b)
        for i in range(len(chain) - 1):
            edge_list.append((chain[i], chain[i + 1],
                              out_idx if i == 0 else 0,
                              in_idx if i == len(chain) - 2 else 0))

    rank_of = dict(rank)
    rank_of.update({w: w.rank for w in waypoints})
    columns = {}
    for n in list(nodes) + waypoints:
        columns.setdefault(rank_of[n], []).append(n)
    ranks = sorted(columns)

    downstream = {}
    upstream = {}
    for u, w, _oi, _ii in edge_list:
        downstream.setdefault(u, []).append(w)
        upstream.setdefault(w, []).append(u)

    # 3a. initial order: depth-first from the outputs, inputs top-to-bottom,
    # so chains start out banded in the BSDF's own socket order
    order = {}

    def visit(node):
        if node in order:
            return
        order[node] = len(order)
        for parent in upstream.get(node, []):
            visit(parent)

    for n in list(nodes) + waypoints:
        if not downstream.get(n):
            visit(n)
    for n in list(nodes) + waypoints:
        visit(n)
    for r in ranks:
        columns[r].sort(key=lambda n: order[n])

    # 3b. crossing reduction: order each column by the barycenter of its
    # already-ordered neighbour column, sweeping both directions
    index = {n: i for r in ranks for i, n in enumerate(columns[r])}
    for sweep in range(4):
        forward = sweep % 2 == 0
        seq = ranks if forward else list(reversed(ranks))
        neighbours = downstream if forward else upstream
        for r in seq[1:]:
            column = columns[r]

            def barycenter(n):
                positions = [index[m] for m in neighbours.get(n, []) if m in index]
                return sum(positions) / len(positions) if positions else index[n]

            column.sort(key=barycenter)
            for i, n in enumerate(column):
                index[n] = i

    # 4a. y: initial stack, then median socket-anchor relaxation with
    # order and margin constraints (forward clamp, lift back, re-clamp)
    y = {}
    for r in ranks:
        cursor = 0.0
        for n in columns[r]:
            y[n] = cursor
            cursor -= node_height(n) + _MARGIN_Y

    edges_of = {}
    for u, w, oi, ii in edge_list:
        edges_of.setdefault(u, []).append((w, _socket_anchor(ii), _socket_anchor(oi)))
        edges_of.setdefault(w, []).append((u, _socket_anchor(oi), _socket_anchor(ii)))

    for iteration in range(6):
        for r in (ranks if iteration % 2 else reversed(ranks)):
            column = columns[r]
            desired = []
            for n in column:
                wants = [y[other] + other_anchor - own_anchor
                         for other, other_anchor, own_anchor in edges_of.get(n, [])]
                wants.sort()
                desired.append(wants[len(wants) // 2] if wants else y[n])
            for i in range(1, len(column)):
                limit = desired[i - 1] - node_height(column[i - 1]) - _MARGIN_Y
                if desired[i] > limit:
                    desired[i] = limit
            for i in range(len(column) - 2, -1, -1):
                limit = desired[i + 1] + node_height(column[i]) + _MARGIN_Y
                if desired[i] < limit:
                    desired[i] = limit
            for i in range(1, len(column)):
                limit = desired[i - 1] - node_height(column[i - 1]) - _MARGIN_Y
                if desired[i] > limit:
                    desired[i] = limit
            for i, n in enumerate(column):
                y[n] = desired[i]

    # 4b. x: columns sized by their widest node, outputs at x = 0
    x = {}
    for r in ranks:
        if r == ranks[0]:
            x[r] = 0.0
        else:
            width = max((node_width(n) for n in columns[r]), default=140.0)
            x[r] = x[r - 1] - width - _MARGIN_X

    for n in nodes:
        n.location = (x[rank_of[n]], y[n])


def _tidy_armatures(context, objects):
    """Make imported skeletons read like rigs: glTF joints have no length, so
    Blender draws stubs. Purely topological - each bone's tail goes to its
    single child's head (average for forks, parent direction for leaves)."""
    for arm_obj in [o for o in objects if o.type == "ARMATURE"]:
        prev_active = context.view_layer.objects.active
        try:
            bpy.ops.object.select_all(action="DESELECT")
        except RuntimeError:
            pass
        arm_obj.select_set(True)
        context.view_layer.objects.active = arm_obj
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bones = arm_obj.data.edit_bones
            for bone in bones:
                children = bone.children
                if len(children) == 1:
                    target = children[0].head
                elif len(children) > 1:
                    target = sum((c.head for c in children), Vector()) / len(children)
                elif bone.parent is not None:
                    direction = (bone.head - bone.parent.head)
                    target = bone.head + (direction.normalized() * max(bone.parent.length * 0.5, 0.02)
                                          if direction.length > 1e-6 else Vector((0, 0.05, 0)))
                else:
                    target = bone.head + Vector((0, 0.1, 0))
                if (target - bone.head).length > 1e-4:
                    bone.tail = target
            for bone in bones:
                if bone.parent is not None and (bone.head - bone.parent.tail).length < 1e-5:
                    bone.use_connect = True
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass
        finally:
            context.view_layer.objects.active = prev_active
        arm_obj.data.display_type = "OCTAHEDRAL"
        arm_obj.show_in_front = True


def _action_bone_names(action):
    """Bone names an action animates (Blender 5 layered-action API)."""
    names = set()
    try:
        for layer in action.layers:
            for strip in layer.strips:
                for slot in action.slots:
                    bag = strip.channelbag(slot)
                    if bag:
                        for fc in bag.fcurves:
                            if 'pose.bones["' in fc.data_path:
                                names.add(fc.data_path.split('"')[1])
    except Exception:
        pass
    return names


def _sequence_clips(objects, actions, prefix):
    """Lay each armature's clips end-to-end on one NLA track: the whole
    library is visible on the timeline in sequence, and pruning a clip is
    deleting its strip - no sifting through the global action list. The
    importer's own one-strip-per-clip stash tracks are folded away."""
    armatures = [o for o in objects if o.type == "ARMATURE"]
    for arm in armatures:
        own = []
        bone_names = {b.name for b in arm.data.bones}
        for action in actions:
            animated = _action_bone_names(action)
            if animated and animated <= bone_names:
                own.append(action)
        if not own:
            continue
        if prefix and prefix.lower() not in arm.name.lower():
            arm.name = f"{prefix}.rig"
        arm.animation_data_create()
        if arm.animation_data.nla_tracks.get("Rust Clips"):
            continue
        own_set = set(own)
        for track in list(arm.animation_data.nla_tracks):
            if track.strips and all(s.action in own_set for s in track.strips):
                arm.animation_data.nla_tracks.remove(track)
        track = arm.animation_data.nla_tracks.new()
        track.name = "Rust Clips"
        frame = 1
        for action in sorted(own, key=lambda a: a.name):
            length = max(1, int(action.frame_range[1] - action.frame_range[0]) + 1)
            strip = track.strips.new(action.name, frame, action)
            strip.extrapolation = "NOTHING"
            frame += length + 10
        # keep the active action empty so the sequence is what plays
        arm.animation_data.action = None


def _import_glb(context, filepath):
    settings = context.scene.rust_ripper
    before_objects = set(bpy.data.objects)
    before_materials = set(bpy.data.materials)
    before_actions = set(bpy.data.actions)
    bpy.ops.import_scene.gltf(filepath=filepath)
    new_objects = [o for o in bpy.data.objects if o not in before_objects]
    new_materials = [m for m in bpy.data.materials if m not in before_materials]
    new_actions = [a for a in bpy.data.actions if a not in before_actions]
    hidden, reused = _post_process(new_objects, settings)
    _tidy_armatures(context, new_objects)
    # namespace actions per import so every rig's clips are findable in the
    # global action list ("wolf2|wolf_run", "chicken|walk")
    # the export root carries unity_prefab_path (contract) - the reliable name
    root = next((o for o in new_objects if o.get("unity_prefab_path")), None) \
        or next((o for o in new_objects if o.parent is None), None)
    prefix = root.name.split(".")[0] if root is not None else ""
    if prefix:
        for action in new_actions:
            if "|" not in action.name:
                action.name = f"{prefix}|{action.name}"
    _sequence_clips(new_objects, new_actions, prefix)
    # animation-target empties are load-bearing (deleting them breaks clips):
    # keep them visible but small
    for obj in new_objects:
        if obj.type == "EMPTY" and obj.get("unity_animated"):
            obj.empty_display_size = 0.05
    painted = _build_paint_nodes(filepath, new_materials)
    painted += _build_blend_layer_nodes(filepath, new_materials, new_objects)
    painted += _build_blend4way_nodes(filepath, new_materials, new_objects)
    painted += _build_decal_clips(new_materials)
    _compact_alpha_clips(new_materials)
    if _count_fur_materials(new_materials) and context.scene.render.engine == "CYCLES":
        # fur shells stack many alpha layers; rays that exhaust Cycles'
        # transparent bounce budget terminate BLACK between the tufts
        cycles = context.scene.cycles
        cycles.transparent_max_bounces = max(cycles.transparent_max_bounces, 64)
    # prune dead nodes first, then lay out: the Node Arrange extension
    # (full Sugiyama - crossing minimization, true node sizes) does the
    # arranging when installed; the built-in column layout is the fallback
    _ensure_weight_attributes(new_objects, new_materials)
    trees = [mat.node_tree for mat in new_materials if mat.use_nodes]
    for tree in trees:
        _prune_unused_nodes(tree)
        _arrange_nodes(tree)
    return len(new_objects), hidden, painted, reused


def _light_info(obj):
    info = obj.get("unity_light")
    return dict(info) if info is not None and hasattr(info, "keys") else None


def _is_fill_light(obj):
    """Heuristic, not ground truth: vertex-mode lights are Rust's cheap fill
    lights; shadowless cookie-less faint lights are usually bounce fakes."""
    info = _light_info(obj)
    if not info:
        return False
    if info.get("unity_render_mode") == 2:
        return True
    return (info.get("unity_shadows", 0) == 0
            and not info.get("unity_cookie")
            and info.get("unity_intensity", 0) <= 2.0)


# ---------------------------------------------------------------- operators

class RUST_OT_import_glb(bpy.types.Operator, ImportHelper):
    bl_idname = "rust.import_glb"
    bl_label = "Rust Ripper GLB (.glb)"
    bl_description = "Import a Rust Ripper GLB with PBR materials, blend layers, visibility and light handling"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})

    def execute(self, context):
        count, hidden, painted, reused = _import_glb(context, self.filepath)
        self.report({"INFO"}, f"{count} objects ({hidden} hidden, {painted} paint, {reused} meshes reused)")
        return {"FINISHED"}


class RUST_OT_check_connection(bpy.types.Operator):
    bl_idname = "rust.check_connection"
    bl_label = "Connect to Bridge"
    bl_description = "Connect to the Rust Ripper daemon bridge"

    def execute(self, context):
        try:
            with urllib.request.urlopen(f"{DAEMON}/status", timeout=5) as response:
                data = json.loads(response.read())
            context.scene.rust_ripper["bridge_connected"] = True
            self.report({"INFO"}, f"Connected — Rust Ripper {data.get('version', '?')}")
        except Exception as e:
            context.scene.rust_ripper["bridge_connected"] = False
            self.report({"WARNING"}, f"Bridge not reachable ({e})")
        return {"FINISHED"}


class RUST_OT_disconnect_bridge(bpy.types.Operator):
    bl_idname = "rust.disconnect_bridge"
    bl_label = "Disconnect Bridge"
    bl_description = "Disconnect from the Rust Ripper bridge"

    def execute(self, context):
        context.scene.rust_ripper["bridge_connected"] = False
        self.report({"INFO"}, "Bridge disconnected")
        return {"FINISHED"}


class RUST_OT_hide_fill_lights(bpy.types.Operator):
    bl_idname = "rust.hide_fill_lights"
    bl_label = "Hide Fill Lights"
    bl_description = (
        "Hide bounce/fill lights (vertex render mode, or shadowless cookie-less faint lights).\n"
        "Light types in Rust:\n"
        "  • Key lights — cast shadows, main scene lighting\n"
        "  • Fill lights — no shadows, low intensity, ambient bounce fakes\n"
        "  • Cookie lights — projected texture (stained glass, caustics)\n"
        "This operator hides fill lights only."
    )

    def execute(self, context):
        count = 0
        for obj in context.scene.objects:
            if obj.type == "LIGHT" and _is_fill_light(obj):
                obj.hide_set(True)
                obj.hide_render = True
                count += 1
        self.report({"INFO"}, f"{count} fill lights hidden")
        return {"FINISHED"}


class RUST_OT_show_all_lights(bpy.types.Operator):
    bl_idname = "rust.show_all_lights"
    bl_label = "Show All Lights"
    bl_description = "Unhide all lights in the scene"

    def execute(self, context):
        for obj in context.scene.objects:
            if obj.type == "LIGHT":
                obj.hide_set(False)
                obj.hide_render = False
        return {"FINISHED"}


# ---------------------------------------------------------------- panels

class RUST_PT_main(bpy.types.Panel):
    bl_label = "Rust Ripper"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rust"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.rust_ripper

        # bridge status — top row
        connected = settings.get("bridge_connected", False)
        row = layout.row(align=True)
        if connected:
            row.operator("rust.disconnect_bridge", text="Disconnect", icon="UNLINKED")
            row.label(text="Bridge active", icon="CHECKBOX_HLT")
        else:
            row.operator("rust.check_connection", text="Connect", icon="LINKED")
            row.label(text="No bridge", icon="CHECKBOX_DEHLT")

        layout.separator()
        layout.prop(settings, "root_display_size")
        layout.prop(settings, "auto_hide")
        layout.prop(settings, "reuse_meshes")
        layout.separator()
        row = layout.row(align=True)
        row.operator("rust.hide_fill_lights", icon="LIGHT_SUN")
        row.operator("rust.show_all_lights", icon="HIDE_OFF")


# ---------------------------------------------------------------- menu hook

def _apply_outliner_viewport_column(hide):
    """Hide the viewport restrict column (monitor icon) in all outliners,
    keeping the hide column (eye icon) visible."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "OUTLINER":
                for space in area.spaces:
                    if space.type == "OUTLINER":
                        space.show_restrict_column_viewport = not hide


def _menu_import(self, context):
    self.layout.operator("rust.import_glb", text="Rust Ripper GLB (.glb)")


classes = (
    RustRipperSettings,
    RUST_OT_import_glb,
    RUST_OT_check_connection,
    RUST_OT_disconnect_bridge,
    RUST_OT_hide_fill_lights,
    RUST_OT_show_all_lights,
    RUST_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.rust_ripper = PointerProperty(type=RustRipperSettings)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    _apply_outliner_viewport_column(True)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    del bpy.types.Scene.rust_ripper
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _apply_outliner_viewport_column(False)


if __name__ == "__main__":
    register()
