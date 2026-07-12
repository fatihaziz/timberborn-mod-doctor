using System.Collections.Generic;
using Timberborn.CameraSystem;
using Timberborn.CoreUI;
using Timberborn.SingletonSystem;
using UnityEngine;
using UnityEngine.UIElements;

namespace ModDoctor.Compat.GrowthOverlay;

internal sealed class GrowthOverlayService : ILoadableSingleton, ILateUpdatableSingleton
{
    private readonly Underlay _underlay;
    private readonly CameraService _cameraService;
    private readonly UISettings _uiSettings;
    private readonly Dictionary<VisualElement, Vector3> _items = new();
    private bool _visible;
    private bool _dirty;

    public GrowthOverlayService(Underlay underlay, CameraService cameraService,
        UISettings uiSettings)
    {
        _underlay = underlay;
        _cameraService = cameraService;
        _uiSettings = uiSettings;
    }

    public bool Visible => _visible;

    public void Load()
    {
        _cameraService.CameraPositionOrRotationChanged += (_, _) => _dirty = true;
        _uiSettings.UIScaleFactorChanged += (_, _) => _dirty = true;
    }

    public void LateUpdateSingleton()
    {
        if (!_dirty) return;
        foreach ((VisualElement item, Vector3 anchor) in _items)
            Position(item, anchor);
        _dirty = false;
    }

    public void Add(VisualElement item, Vector3 anchor)
    {
        if (!_items.TryAdd(item, anchor)) return;
        if (_visible) _underlay.Add(item);
        _dirty = true;
    }

    public void Remove(VisualElement item)
    {
        if (!_items.Remove(item) || !_visible) return;
        _underlay.Remove(item);
    }

    public void SetVisible(bool visible)
    {
        if (_visible == visible) return;
        _visible = visible;
        foreach (VisualElement item in _items.Keys)
        {
            if (visible) _underlay.Add(item);
            else _underlay.Remove(item);
        }
        _dirty = visible;
    }

    private void Position(VisualElement item, Vector3 anchor)
    {
        if (!_visible || item.panel == null) return;
        bool inFront = _cameraService.IsInFront(anchor);
        Timberborn.CoreUI.VisualElementExtensions.ToggleDisplayStyle(item, inFront);
        if (!inFront) return;
        VisualElement root = _underlay.Root;
        Vector3 panel = _cameraService.WorldSpaceToPanelSpace(root, anchor);
        item.style.left = panel.x - root.layout.width / 2f;
        item.style.top = panel.y - root.layout.height / 2f;
    }
}
