using Timberborn.AreaSelectionSystem;
using Timberborn.AreaSelectionSystemUI;
using Timberborn.BlockSystem;
using Timberborn.Buildings;
using Timberborn.Emptying;
using Timberborn.Hauling;
using Timberborn.InputSystem;

namespace ModDoctor.Compat.DraggableUtils;

internal sealed class PauseTool : DraggableTool
{
    public PauseTool(AreaBlockObjectPickerFactory pickerFactory, InputService inputService,
        BlockObjectSelectionDrawerFactory drawerFactory, CursorService cursorService)
        : base(pickerFactory, inputService, drawerFactory, cursorService) { }

    protected override bool IsEligible(BlockObject blockObject)
    {
        PausableBuilding component = blockObject.GetComponent<PausableBuilding>();
        return component != null && component.IsPausable();
    }

    protected override void ApplyTo(BlockObject blockObject)
    {
        PausableBuilding component = blockObject.GetComponent<PausableBuilding>();
        if (component == null || !component.IsPausable()) return;
        if (ShiftHeld) component.Resume();
        else component.Pause();
    }
}

internal sealed class HaulPrioritizeTool : DraggableTool
{
    public HaulPrioritizeTool(AreaBlockObjectPickerFactory pickerFactory, InputService inputService,
        BlockObjectSelectionDrawerFactory drawerFactory, CursorService cursorService)
        : base(pickerFactory, inputService, drawerFactory, cursorService) { }

    protected override bool IsEligible(BlockObject blockObject) =>
        blockObject.GetComponent<HaulPrioritizable>() != null;

    protected override void ApplyTo(BlockObject blockObject)
    {
        HaulPrioritizable component = blockObject.GetComponent<HaulPrioritizable>();
        if (component != null) component.Prioritized = !ShiftHeld;
    }
}

internal sealed class EmptyStorageTool : DraggableTool
{
    public EmptyStorageTool(AreaBlockObjectPickerFactory pickerFactory, InputService inputService,
        BlockObjectSelectionDrawerFactory drawerFactory, CursorService cursorService)
        : base(pickerFactory, inputService, drawerFactory, cursorService) { }

    protected override bool IsEligible(BlockObject blockObject) =>
        blockObject.GetComponent<Emptiable>() != null;

    protected override void ApplyTo(BlockObject blockObject)
    {
        Emptiable component = blockObject.GetComponent<Emptiable>();
        if (component == null) return;
        if (ShiftHeld) component.UnmarkForEmptying();
        else component.MarkForEmptyingWithStatus();
    }
}
