using System;
using Bindito.Core;
using Timberborn.BaseComponentSystem;
using Timberborn.BlockSystem;
using Timberborn.CoreUI;
using Timberborn.EntitySystem;
using Timberborn.Gathering;
using Timberborn.Growing;
using Timberborn.NaturalResourcesLifecycle;
using Timberborn.SelectionSystem;
using Timberborn.TickSystem;
using UnityEngine;
using UnityEngine.UIElements;

namespace ModDoctor.Compat.GrowthOverlay;

internal sealed class GrowthOverlayItem : TickableComponent, IInitializableEntity, IDeletableEntity
{
    private GrowthOverlayService _overlay = null!;
    private VisualElementLoader _visualElementLoader = null!;
    private EntitySelectionService _selectionService = null!;
    private BlockObjectCenter _center = null!;
    private Growable _growable = null!;
    private GatherableYieldGrower? _yieldGrower;
    private LivingNaturalResource _resource = null!;
    private VisualElement _item = null!;
    private Label _text = null!;

    [Inject]
    public void InjectDependencies(GrowthOverlayService overlay,
        VisualElementLoader visualElementLoader, EntitySelectionService selectionService)
    {
        _overlay = overlay;
        _visualElementLoader = visualElementLoader;
        _selectionService = selectionService;
    }

    public void Awake()
    {
        _center = GetComponent<BlockObjectCenter>();
        _growable = GetComponent<Growable>();
        _yieldGrower = GetComponent<GatherableYieldGrower>();
        _resource = GetComponent<LivingNaturalResource>();
        _item = _visualElementLoader.LoadVisualElement("Game/StockpileOverlayItem");
        _item.Q<Button>("EntityButton").clicked += Select;
        _item.Q<Button>("SelectionButton").clicked += Select;
        _item.Q<Image>("Icon").AddToClassList("icon--hidden");
        _text = _item.Q<Label>("Stock");
        VisualElement? progressWrapper = _item.Q<VisualElement>("Progress")?.parent;
        progressWrapper?.parent?.Remove(progressWrapper);
    }

    public void InitializeEntity()
    {
        if (_resource.IsDead) return;
        Vector3 center = _center.WorldCenter;
        Vector3 grounded = _center.WorldCenterGrounded;
        _overlay.Add(_item, new Vector3(center.x, (center.y + grounded.y) * 0.5f, center.z));
        UpdateText();
    }

    public void DeleteEntity() => _overlay.Remove(_item);

    public override void Tick()
    {
        if (_overlay.Visible && !_resource.IsDead) UpdateText();
    }

    private void Select() => _selectionService.Select(_growable);

    private void UpdateText()
    {
        float progress = _growable.GrowthProgress * 100f;
        if (_growable.IsGrown && _yieldGrower != null)
            progress = 100f + _yieldGrower.GrowthProgress * 100f;
        _text.text = $"{MathF.Floor(progress)}%";
        Timberborn.CoreUI.VisualElementExtensions.ToggleDisplayStyle(_text, true);
    }
}
