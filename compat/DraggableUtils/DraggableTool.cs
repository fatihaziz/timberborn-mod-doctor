using System.Collections.Generic;
using Timberborn.AreaSelectionSystem;
using Timberborn.AreaSelectionSystemUI;
using Timberborn.BlockSystem;
using Timberborn.InputSystem;
using Timberborn.ToolSystem;
using UnityEngine;

namespace ModDoctor.Compat.DraggableUtils;

internal abstract class DraggableTool : ITool, IInputProcessor
{
    private readonly AreaBlockObjectPicker _picker;
    private readonly CursorService _cursorService;
    private readonly BlockObjectSelectionDrawer _previewDrawer;
    private readonly BlockObjectSelectionDrawer _actionDrawer;

    protected DraggableTool(AreaBlockObjectPickerFactory pickerFactory, InputService inputService,
        BlockObjectSelectionDrawerFactory drawerFactory, CursorService cursorService)
    {
        InputService = inputService;
        _cursorService = cursorService;
        _picker = pickerFactory.CreatePickingUpwards();
        _previewDrawer = drawerFactory.Create(
            new Color(1f, 0.75f, 0.1f, 1f), new Color(1f, 0.75f, 0.1f, 0.35f),
            new Color(1f, 0.5f, 0.05f, 0.65f));
        _actionDrawer = drawerFactory.Create(
            new Color(0.1f, 0.85f, 0.3f, 1f), new Color(0.1f, 0.85f, 0.3f, 0.35f),
            new Color(0.05f, 0.6f, 0.2f, 0.65f));
    }

    protected InputService InputService { get; }
    protected static bool ShiftHeld =>
        Input.GetKey(KeyCode.LeftShift) || Input.GetKey(KeyCode.RightShift);

    public bool ProcessInput() =>
        _picker.PickBlockObjects<BlockObject>(Preview, Apply, Hide, IsEligible);

    public void Enter() => InputService.AddInputProcessor(this);

    public void Exit()
    {
        _picker.Reset();
        _cursorService.ResetCursor();
        _previewDrawer.StopDrawing();
        _actionDrawer.StopDrawing();
        InputService.RemoveInputProcessor(this);
    }

    protected abstract bool IsEligible(BlockObject blockObject);
    protected abstract void ApplyTo(BlockObject blockObject);

    private void Preview(IEnumerable<BlockObject> blockObjects, Vector3Int start, Vector3Int end,
        bool selectionStarted, bool selectingArea)
    {
        IEnumerable<BlockObject> eligible = Filter(blockObjects);
        if (selectionStarted || selectingArea)
            _actionDrawer.Draw(eligible, start, end, selectingArea);
        else
            _previewDrawer.Draw(eligible, start, end, false);
    }

    private void Apply(IEnumerable<BlockObject> blockObjects, Vector3Int start, Vector3Int end,
        bool selectionStarted, bool selectingArea)
    {
        foreach (BlockObject blockObject in Filter(blockObjects))
            ApplyTo(blockObject);
    }

    private IEnumerable<BlockObject> Filter(IEnumerable<BlockObject> blockObjects)
    {
        foreach (BlockObject blockObject in blockObjects)
            if (IsEligible(blockObject))
                yield return blockObject;
    }

    private void Hide()
    {
        _previewDrawer.StopDrawing();
        _actionDrawer.StopDrawing();
    }
}
