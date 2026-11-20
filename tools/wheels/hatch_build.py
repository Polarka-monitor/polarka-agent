"""A wheel with the proof-of-work library inside is a wheel for one platform. tools/wheels/build-*.sh name the
platform in POLARKA_WHEEL_PLATFORM (for example manylinux_2_28_x86_64); without it the wheel is the plain one,
for every platform, without the library."""
import os

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        platform = os.environ.get("POLARKA_WHEEL_PLATFORM")
        lib = os.path.join(self.root, "polarka_agent", "lib")
        has_lib = os.path.isdir(lib) and any(n.startswith("librxnorth.") for n in os.listdir(lib))
        if platform and not has_lib:
            raise RuntimeError("POLARKA_WHEEL_PLATFORM is set but polarka_agent/lib holds no librxnorth")
        if has_lib and not platform:
            raise RuntimeError("polarka_agent/lib holds a librxnorth: name its platform in POLARKA_WHEEL_PLATFORM, or remove it")
        if platform:
            build_data["pure_python"] = False
            build_data["tag"] = "py3-none-" + platform
