import SwiftUI

struct PlatformSelectionView: View {
    @ObservedObject var viewModel: PlatformViewModel
    @Environment(\.dismiss) var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(I18nService.shared.translate("popover.selectPlatforms"))
                .font(.headline)
                .padding(.bottom, 8)

            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(PlatformInstanceStore.shared.instances) { instance in
                        Toggle(isOn: Binding(
                            get: { instance.isEnabled },
                            set: { newValue in
                                PlatformManager.shared.setPlatformEnabled(newValue, for: instance)
                                if newValue {
                                    viewModel.switchActiveInstance(instance)
                                }
                                Task { await viewModel.fetchAllUsage() }
                            }
                        )) {
                            HStack {
                                Text(instance.displayTitle)
                                    .font(.body)
                                Spacer()
                            }
                        }
                        .disabled(PlatformManager.shared.isLastEnabledInstance(instance) && instance.isEnabled)
                    }
                }
            }

            if PlatformInstanceStore.shared.instances.allSatisfy({ !$0.isEnabled }) {
                Text(I18nService.shared.translate("popover.atLeastOnePlatform"))
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .padding()
        .frame(width: 250, height: 280)
    }
}
