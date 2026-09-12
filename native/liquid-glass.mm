// Public AppKit NSGlassEffectView bridge, compiled against the macOS 26 SDK.
#import <AppKit/AppKit.h>
#import <objc/runtime.h>
#include <node_api.h>
#include <cstring>

API_AVAILABLE(macos(26.0))
@interface MediaDropGlassView : NSGlassEffectView
@end
@implementation MediaDropGlassView
- (NSView *)hitTest:(NSPoint)point { return nil; }
@end

static const char glassKey = 0;
static napi_value Apply(napi_env env, napi_callback_info info) {
    napi_value args[3], result;
    size_t argc = 3, length = 0;
    void *bytes = nullptr;
    bool enabled = false, buffer = false;
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
    if (argc != 3 || napi_is_buffer(env, args[0], &buffer) != napi_ok || !buffer ||
        napi_get_buffer_info(env, args[0], &bytes, &length) != napi_ok || length != sizeof(void *) ||
        napi_get_value_bool(env, args[1], &enabled) != napi_ok || ![NSThread isMainThread]) {
        napi_throw_type_error(env, nullptr, "Invalid native glass invocation"); return nullptr;
    }
    char appearance[16] = {0};
    napi_get_value_string_utf8(env, args[2], appearance, sizeof(appearance), nullptr);
    bool active = false;
    if (@available(macOS 26.0, *)) {
        NSView *root = (__bridge NSView *)(*reinterpret_cast<void **>(bytes));
        if (root && root.window) {
            MediaDropGlassView *glass = objc_getAssociatedObject(root, &glassKey);
            enabled = enabled && !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceTransparency
                && !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldIncreaseContrast;
            if (enabled && !glass) {
                glass = [[MediaDropGlassView alloc] initWithFrame:root.bounds];
                glass.style = NSGlassEffectViewStyleRegular;
                glass.cornerRadius = 0;
                glass.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
                [root addSubview:glass positioned:NSWindowBelow relativeTo:nil];
                objc_setAssociatedObject(root, &glassKey, glass, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
            }
            if (glass) {
                glass.hidden = !enabled;
                glass.appearance = !strcmp(appearance, "dark") ? [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua]
                    : !strcmp(appearance, "light") ? [NSAppearance appearanceNamed:NSAppearanceNameAqua] : nil;
                active = enabled;
            }
        }
    }
    napi_get_boolean(env, active, &result);
    return result;
}
static napi_value Init(napi_env env, napi_value exports) {
    napi_value fn; napi_create_function(env, "apply", NAPI_AUTO_LENGTH, Apply, nullptr, &fn);
    napi_set_named_property(env, exports, "apply", fn); return exports;
}
NAPI_MODULE(NODE_GYP_MODULE_NAME, Init)
