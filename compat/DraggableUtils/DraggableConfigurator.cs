using Bindito.Core;
using Timberborn.BottomBarSystem;

namespace ModDoctor.Compat.DraggableUtils;

[Context("Game")]
public sealed class DraggableConfigurator : Configurator
{
    protected override void Configure()
    {
        Bind<PauseTool>().AsSingleton();
        Bind<HaulPrioritizeTool>().AsSingleton();
        Bind<EmptyStorageTool>().AsSingleton();
        Bind<DraggableButton>().AsSingleton();
        MultiBind<BottomBarModule>().ToProvider<ModuleProvider>().AsSingleton();
    }

    private sealed class ModuleProvider : IProvider<BottomBarModule>
    {
        private readonly DraggableButton _button;

        public ModuleProvider(DraggableButton button)
        {
            _button = button;
        }

        public BottomBarModule Get()
        {
            var builder = new BottomBarModule.Builder();
            builder.AddLeftSectionElement(_button, 100);
            return builder.Build();
        }
    }
}
