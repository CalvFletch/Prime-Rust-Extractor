namespace RustRipper.Core;

/// <summary>
/// Layer 2 interpretation table (docs/ARCHITECTURE.md): how a shader's
/// declared property interface maps onto glTF channels. Keyed on the shader
/// asset name — the material's type identity. Adding support for a shader is
/// adding a row here, never a branch in the factory.
/// </summary>
public sealed record ShaderProfile
{
    public required string Id { get; init; }

    /// <summary>Exact shader names this profile covers.</summary>
    public string[] Shaders { get; init; } = [];

    public string[] BaseColorSlots { get; init; } = [];
    public string[] NormalSlots { get; init; } = [];

    /// <summary>Unity metal-gloss packing: R=metal, A=smoothness.</summary>
    public string[] MetalGlossSlots { get; init; } = [];

    /// <summary>Specular workflow: RGB=F0 (also exported as KHR specularColorTexture), A=smoothness.</summary>
    public string[] SpecGlossSlots { get; init; } = [];

    /// <summary>Rust packed ORM: G=gloss, B=metal, A=AO.</summary>
    public string[] PackedOrmSlots { get; init; } = [];

    public string[] OcclusionSlots { get; init; } = [];
    public string[] EmissiveSlots { get; init; } = [];

    /// <summary>Every shipped fragment variant alpha-tests (discard below
    /// _Cutoff) without declaring Unity's _Mode float. AnimalFur ships only
    /// _OPACITYMASK_ON variants - materials do not opt in via keywords.</summary>
    public bool UsesAlphaTest { get; init; }

    /// <summary>Shader implements the detail-layer paint system (_DetailLayer/_DetailMask/_DetailColor).</summary>
    public bool SupportsDetailPaint { get; init; }

    /// <summary>Shader implements the colorize system (_ColorizeLayer/_ColorizeMask/_ColorizeColor*).</summary>
    public bool SupportsColorize { get; init; }

    /// <summary>_SmoothnessTextureChannel==1 routes smoothness from the albedo alpha.</summary>
    public bool SupportsAlbedoAlphaSmoothness { get; init; }

    /// <summary>The shader's compiled programs use vertex colour exclusively
    /// as layer weights - no vertex-tint path exists (no such keyword in the
    /// shipped variants), so _ApplyVertexColor is declared-but-unused and
    /// COLOR_0 must always demote to _RUST_COLOR.</summary>
    public bool VertexColorIsLayerWeights { get; init; }
}

public static class ShaderProfiles
{
    /// <summary>
    /// The Unity/Rust Standard family. Also the FALLBACK for unmapped shaders:
    /// best-effort standard interpretation plus the complete property dump in
    /// extras. Unmapped shaders are surfaced by `ripper coverage`.
    /// </summary>
    public static readonly ShaderProfile Standard = new()
    {
        Id = "standard",
        Shaders =
        [
            "Rust/Standard",
            "Rust/Standard (Specular setup)",
            "Standard",
            "Autodesk Interactive",
            "Rust/Standard Blend Layer",
            "Rust/Standard Blend Layer (Specular setup)",
            "Rust/Standard Cloth",
            "Rust/Standard Cloth (Specular setup)",
            "Rust/Standard Decal",
            "Rust/Standard Decal (Specular setup)",
            "Rust/Standard Decal (Poster)",
            "Rust/Standard + Wind",
            "Rust/Standard + Wind (Specular setup)",
            "Rust/Standard Terrain Blend (Specular setup)",
            // program-verified (shaderdump): standard slots + numbered blend
            // layers; the packed-mask macro system rides in extras
            "Developer/LocalCoord Diffuse (Specular Setup)",
            "Developer/LocalCoord Diffuse (Metallic Setup)",
            "Custom/Standard Refraction",
            "Rust/Flare",
            "Particles/VertexLit Blended Custom",
            "Particles/Additive (HDR)",
        ],
        BaseColorSlots = ["_MainTex", "_BaseColorMap", "_AlbedoMap"],
        NormalSlots = ["_BumpMap", "_NormalMap", "_Normal"],
        MetalGlossSlots = ["_MetallicGlossMap"],
        SpecGlossSlots = ["_SpecGlossMap", "_SpecularMap"],
        PackedOrmSlots = ["_PackedMap"],
        OcclusionSlots = ["_OcclusionMap"],
        EmissiveSlots = ["_EmissionMap"],
        SupportsDetailPaint = true,
        SupportsColorize = true,
        SupportsAlbedoAlphaSmoothness = true,
    };

    /// <summary>Fur shells. Read from the compiled programs: the albedo's OWN
    /// alpha carries the tuft transparency (alpha-tested at _Cutoff after a
    /// lerp toward linearized vertex red); _FuzzMask only shades the fuzz
    /// COLOUR term and rides in extras.</summary>
    public static readonly ShaderProfile AnimalFur = new()
    {
        Id = "animal-fur",
        Shaders = ["AnimalFur"],
        BaseColorSlots = ["_Diffuse"],
        SpecGlossSlots = ["_Specular"],
        OcclusionSlots = ["_AO"],
        UsesAlphaTest = true,
    };

    /// <summary>Rust's foliage system. Slot mapping is provisional — the snow
    /// and wind data live in foliage-specific parameters not yet mapped.</summary>
    public static readonly ShaderProfile CoreFoliage = new()
    {
        Id = "core-foliage",
        Shaders = ["Core/Foliage", "Core/Foliage Billboard"],
        BaseColorSlots = ["_BaseColorMap", "_MainTex"],
        NormalSlots = ["_NormalMap", "_BumpMap"],
    };

    /// <summary>Projected decals. Slots read from the compiled programs
    /// (shaderdump): _MainTex/_BumpMap/_SpecGlossMap/_EmissionMap plus a
    /// dedicated _AlphaTex; the *Copy slots are gbuffer reads, not material
    /// data. Statically a decal is its quad - blend state supplies alpha.</summary>
    public static readonly ShaderProfile DeferredDecal = new()
    {
        Id = "deferred-decal",
        Shaders = ["Decal/Deferred Decal"],
        BaseColorSlots = ["_MainTex"],
        NormalSlots = ["_BumpMap"],
        SpecGlossSlots = ["_SpecGlossMap"],
        EmissiveSlots = ["_EmissionMap"],
    };

    /// <summary>NPC/player skin. Slots read from the compiled programs:
    /// _ScatterMap (subsurface), hair packed maps and detail rough/normal
    /// have no glTF channel and ride in extras.</summary>
    public static readonly ShaderProfile CoreSkin = new()
    {
        Id = "core-skin",
        Shaders = ["Core/Skin"],
        BaseColorSlots = ["_BaseColorMap"],
        NormalSlots = ["_NormalMap"],
        SpecGlossSlots = ["_SpecularMap"],
    };

    /// <summary>The numbered-blend-layer family. Identical slot layout to
    /// Standard, but the compiled programs have NO vertex-tint path (the
    /// shipped keyword sets contain no such variant; the asm reads COLOR
    /// only as layer weights r/g/b) - materials may still author
    /// _ApplyVertexColor=1, and it is declared-but-unused there.</summary>
    public static readonly ShaderProfile BlendLayerWeights = Standard with
    {
        Id = "blend-layer-weights",
        Shaders =
        [
            "Rust/Standard Blend 4-Way",
            "Rust/Standard Blend 4-Way (Specular setup)",
            "Rust/Standard Packed Mask Blend",
        ],
        VertexColorIsLayerWeights = true,
    };

    public static readonly ShaderProfile[] All = [Standard, BlendLayerWeights, AnimalFur, CoreFoliage, DeferredDecal, CoreSkin];

    private static readonly Dictionary<string, ShaderProfile> byShader = Build();

    private static Dictionary<string, ShaderProfile> Build()
    {
        var map = new Dictionary<string, ShaderProfile>(StringComparer.Ordinal);
        foreach (var profile in All)
        {
            foreach (var shader in profile.Shaders)
            {
                map[shader] = profile;
            }
        }
        return map;
    }

    public static ShaderProfile Resolve(string shaderName)
        => byShader.GetValueOrDefault(shaderName, Standard);

    /// <summary>True when the shader is explicitly covered by a profile row.</summary>
    public static bool IsMapped(string shaderName) => byShader.ContainsKey(shaderName);
}
