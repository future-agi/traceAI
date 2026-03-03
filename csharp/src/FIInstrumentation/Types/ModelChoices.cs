namespace FIInstrumentation.Types;

public enum ModelChoices
{
    TuringLarge,
    TuringSmall,
    Protect,
    ProtectFlash,
    TuringFlash,
}

public static class ModelChoicesExtensions
{
    public static string ToValue(this ModelChoices model) => model switch
    {
        ModelChoices.TuringLarge => "turing_large",
        ModelChoices.TuringSmall => "turing_small",
        ModelChoices.Protect => "protect",
        ModelChoices.ProtectFlash => "protect_flash",
        ModelChoices.TuringFlash => "turing_flash",
        _ => "turing_large",
    };
}
