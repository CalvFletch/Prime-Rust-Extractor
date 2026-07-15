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

    /// <summary>Mask whose red channel becomes base-color alpha (fur shells).</summary>
    public string? FuzzMaskSlot { get; init; }

    /// <summary>Shader implements the detail-layer paint system (_DetailLayer/_DetailMask/_DetailColor).</summary>
    public bool SupportsDetailPaint { get; init; }

    /// <summary>Shader implements the colorize system (_ColorizeLayer/_ColorizeMask/_ColorizeColor*).</summary>
    public bool SupportsColorize { get; init; }

    /// <summary>_SmoothnessTextureChannel==1 routes smoothness from the albedo alpha.</summary>
    public bool SupportsAlbedoAlphaSmoothness { get; init; }
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
            "Rust/Standard Blend 4-Way",
            "Rust/Standard Blend 4-Way (Specular setup)",
            "Standard",
            "Autodesk Interactive",
            "Rust/Standard Blend Layer",
            "Rust/Standard Cloth",
            "Rust/Standard Cloth (Specular setup)",
            "Rust/Standard Decal",
            "Rust/Standard Decal (Specular setup)",
            "Rust/Standard + Wind",
            "Rust/Standard + Wind (Specular setup)",
            "Rust/Standard Terrain Blend (Specular setup)",
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

    public static readonly ShaderProfile AnimalFur = new()
    {
        Id = "animal-fur",
        Shaders = ["AnimalFur"],
        BaseColorSlots = ["_Diffuse"],
        SpecGlossSlots = ["_Specular"],
        OcclusionSlots = ["_AO"],
        FuzzMaskSlot = "_FuzzMask",
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

    public static readonly ShaderProfile[] All = [Standard, AnimalFur, CoreFoliage];

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
