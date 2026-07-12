using System.Collections.Generic;
using Timberborn.BottomBarSystem;
using Timberborn.ToolButtonSystem;
using Timberborn.ToolSystem;

namespace ModDoctor.Compat.DraggableUtils;

internal sealed class DraggableButton : IBottomBarElementsProvider
{
    private const string GroupId = "DraggableUtils";
    private readonly PauseTool _pauseTool;
    private readonly HaulPrioritizeTool _haulTool;
    private readonly EmptyStorageTool _emptyTool;
    private readonly ToolButtonFactory _toolButtonFactory;
    private readonly ToolGroupButtonFactory _groupButtonFactory;
    private readonly ToolGroupService _groupService;

    public DraggableButton(PauseTool pauseTool, HaulPrioritizeTool haulTool,
        EmptyStorageTool emptyTool, ToolButtonFactory toolButtonFactory,
        ToolGroupButtonFactory groupButtonFactory, ToolGroupService groupService)
    {
        _pauseTool = pauseTool;
        _haulTool = haulTool;
        _emptyTool = emptyTool;
        _toolButtonFactory = toolButtonFactory;
        _groupButtonFactory = groupButtonFactory;
        _groupService = groupService;
    }

    public IEnumerable<BottomBarElement> GetElements()
    {
        ToolGroupSpec group = _groupService.GetGroup(GroupId);
        ToolGroupButton groupButton = _groupButtonFactory.CreateBlue(group);
        Add(_pauseTool, "CancelToolIcon", group, groupButton);
        Add(_haulTool, "DemolishResourcesTool", group, groupButton);
        Add(_emptyTool, "DeleteRecoveredGoodStackToolIcon", group, groupButton);
        yield return BottomBarElement.CreateMultiLevel(
            groupButton.Root, groupButton.ToolButtonsElement);
    }

    private void Add(ITool tool, string imageName, ToolGroupSpec group,
        ToolGroupButton groupButton)
    {
        ToolButton button = _toolButtonFactory.Create(
            tool, imageName, groupButton.ToolButtonsElement);
        groupButton.AddTool(button);
        _groupService.AssignToGroup(group, tool);
    }
}
