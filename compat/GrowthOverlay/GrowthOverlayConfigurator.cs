using Bindito.Core;
using Timberborn.Growing;
using Timberborn.TemplateInstantiation;

namespace ModDoctor.Compat.GrowthOverlay;

[Context("Game")]
public sealed class GrowthOverlayConfigurator : Configurator
{
    protected override void Configure()
    {
        Bind<GrowthOverlayService>().AsSingleton();
        Bind<GrowthOverlayInput>().AsSingleton();
        MultiBind<TemplateModule>().ToProvider<TemplateModuleProvider>().AsSingleton();
    }

    private sealed class TemplateModuleProvider : IProvider<TemplateModule>
    {
        public TemplateModule Get()
        {
            var builder = new TemplateModule.Builder();
            builder.AddDecorator<Growable, GrowthOverlayItem>();
            return builder.Build();
        }
    }
}
