using Timberborn.InputSystem;
using Timberborn.SingletonSystem;
using UnityEngine;

namespace ModDoctor.Compat.GrowthOverlay;

internal sealed class GrowthOverlayInput : ILoadableSingleton, IInputProcessor
{
    private readonly GrowthOverlayService _overlay;
    private readonly InputService _inputService;
    private bool _shown;

    public GrowthOverlayInput(GrowthOverlayService overlay, InputService inputService)
    {
        _overlay = overlay;
        _inputService = inputService;
    }

    public void Load() => _inputService.AddInputProcessor(this);

    public bool ProcessInput()
    {
        bool show = Input.GetKey(KeyCode.Tab);
        if (show == _shown) return false;
        _shown = show;
        _overlay.SetVisible(show);
        return false;
    }
}
