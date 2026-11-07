// Process-local experimental module. Explicit installation only; no constructor.
// Build: clang -fobjc-arc -dynamiclib -framework AppKit OfficeCursorScaler.m -o OfficeCursorScaler.dylib
// API status: 0 success; -1 wrong host; -2 wrong thread/not installed;
// -3 incompatible runtime; -4 invalid input; -5 hook ownership conflict.
#import <AppKit/AppKit.h>
#import <objc/runtime.h>
#import <CommonCrypto/CommonDigest.h>
#import <dlfcn.h>
#import <math.h>

static NSString *const UUNotification = @"local.codex.UUDisplayAutomation.OfficeCursorProfile";
static NSString *const UUClientProbe = @"local.codex.UUDisplayAutomation.OfficeCursorClientProbe";
static NSString *const UUClientStatus = @"local.codex.UUDisplayAutomation.OfficeCursorClientStatus";
int UUOfficeCursorInstall(void);
static void UUReportClient(NSString *request);
static void (*UUOriginal)(id, SEL);
static Class UUTarget;
static BOOL UUInstalled, UUEnabled, UUConflict;
static NSString *UUModuleError;
static double UUScale = 3, UUExpires;
static SEL UUSetter;
static int32_t (*UUSeed)(void);
static dispatch_source_t UUExpiryTimer;
static id UUObserver;
static BOOL UUProfileObserving;
static NSMutableDictionary<NSString *, NSCursor *> *UUCache;
static NSMapTable<NSCursor *, NSCursor *> *UUSources;
static NSCursor *UUOwned, *UUOwnedSource;
static NSString *UUOwnedPixels;
static int32_t UUOwnedSeed;
static BOOL UUHasSeed;
#ifdef UU_OFFICE_CURSOR_TESTING
static BOOL UUTestHost;
static Class UUTestClass;
static NSCursor *(*UUTestCurrent)(void);
static NSCursor *(*UUTestAppCurrent)(void);
static BOOL (*UUTestForeground)(void);
static double (*UUTestNow)(void);
static NSUInteger UUTestRequests;
static NSMutableArray<NSDictionary *> *UUTestReports;
static BOOL UUTestRejectSetter;
#endif

static double UUNow(void) {
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestNow) return UUTestNow();
#endif
    return NSDate.date.timeIntervalSince1970;
}
static BOOL UUForeground(void) {
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestHost) return UUTestForeground && UUTestForeground();
#endif
    return NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier == getpid();
}
static NSCursor *UUCurrent(void) {
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestHost) return UUTestCurrent ? UUTestCurrent() : nil;
#endif
    return NSCursor.currentSystemCursor;
}
static NSCursor *UUAppCurrent(void) {
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestHost) return UUTestAppCurrent ? UUTestAppCurrent() : nil;
#endif
    return NSCursor.currentCursor;
}
static BOOL UUValidScale(double scale) {
    return isfinite(scale) && (scale == 1 || (scale >= 1.5 && scale <= 4 && floor(scale * 2) == scale * 2));
}
static BOOL UUAllowedHost(NSString *identifier) {
    return [identifier isEqual:@"com.microsoft.Excel"] ||
           [identifier isEqual:@"com.microsoft.Powerpoint"];
}
static BOOL UUCustom(NSCursor *cursor) {
    SEL selector = NSSelectorFromString(@"_coreCursorType");
    Method method = class_getInstanceMethod(cursor.class, selector);
    if (!method || method_getNumberOfArguments(method) != 2) return NO;
    char type[32] = {0}; method_getReturnType(method, type, sizeof(type));
    if (strcmp(type, @encode(NSInteger)) != 0) return NO;
    return ((NSInteger (*)(id, SEL))method_getImplementation(method))(cursor, selector) == NSIntegerMax;
}
static NSString *UUPixels(NSImage *image) {
    CGImageRef largest = NULL;
    for (NSImageRep *rep in image.representations) {
        if (![rep isKindOfClass:NSBitmapImageRep.class]) continue;
        CGImageRef candidate = ((NSBitmapImageRep *)rep).CGImage;
        if (candidate && (!largest || CGImageGetWidth(candidate) * CGImageGetHeight(candidate) >
                          CGImageGetWidth(largest) * CGImageGetHeight(largest))) largest = candidate;
    }
    if (!largest) return nil;
    size_t width = CGImageGetWidth(largest), height = CGImageGetHeight(largest);
    if (!width || !height || width > 2048 || height > 2048) return nil;
    CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(NULL, width, height, 8, width * 4, space,
        kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(space);
    if (!context) return nil;
    CGContextDrawImage(context, CGRectMake(0, 0, width, height), largest);
    unsigned char hash[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(CGBitmapContextGetData(context), (CC_LONG)(width * height * 4), hash);
    CGContextRelease(context);
    NSMutableString *key = [NSMutableString stringWithFormat:@"%zux%zu:", width, height];
    for (NSUInteger i = 0; i < sizeof(hash); i++) [key appendFormat:@"%02x", hash[i]];
    return key;
}
static NSCursor *UUScaled(NSCursor *source) {
    NSSize size = source.image.size;
    NSPoint hot = source.hotSpot;
    if (!isfinite(size.width) || !isfinite(size.height) || size.width <= 0 || size.height <= 0 ||
        !isfinite(hot.x) || !isfinite(hot.y)) return nil;
    // All representations participate in cache identity, including Retina data.
    NSData *data = source.image.TIFFRepresentation;
    if (!data || data.length > 32 * 1024 * 1024) return nil;
    unsigned char hash[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, hash);
    NSMutableString *key = [NSMutableString new];
    for (NSUInteger i = 0; i < sizeof(hash); i++) [key appendFormat:@"%02x", hash[i]];
    [key appendFormat:@":%.17g:%.17g:%.17g:%.17g:%.1f", size.width, size.height, hot.x, hot.y, UUScale];
    NSCursor *cached = UUCache[key];
    if (cached) return cached;
    NSSize scaledSize = NSMakeSize(size.width * UUScale, size.height * UUScale);
    NSImage *image = [[NSImage alloc] initWithSize:scaledSize];
    if (!source.image.representations.count) return nil;
    for (NSImageRep *rep in source.image.representations) {
        // Reject unsupported vector/animated representations; never partially scale.
        if (![rep isKindOfClass:NSBitmapImageRep.class]) return nil;
        NSInteger width = llround(rep.pixelsWide * UUScale), height = llround(rep.pixelsHigh * UUScale);
        if (rep.pixelsWide <= 0 || rep.pixelsHigh <= 0 || width <= 0 || height <= 0 || width > 2048 || height > 2048) return nil;
        NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL
            pixelsWide:width pixelsHigh:height bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES
            isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
        NSGraphicsContext *context = [NSGraphicsContext graphicsContextWithBitmapImageRep:bitmap];
        if (!context) return nil;
        [NSGraphicsContext saveGraphicsState];
        @try {
            NSGraphicsContext.currentContext = context;
            context.imageInterpolation = NSImageInterpolationHigh;
            [rep drawInRect:NSMakeRect(0, 0, width, height)];
        } @finally {
            [NSGraphicsContext restoreGraphicsState];
        }
        bitmap.size = scaledSize;
        [image addRepresentation:bitmap];
    }
    NSCursor *replacement = [[NSCursor alloc] initWithImage:image hotSpot:NSMakePoint(hot.x * UUScale, hot.y * UUScale)];
    if (UUCache.count >= 32) [UUCache removeAllObjects];
    UUCache[key] = replacement;
    [UUSources setObject:source forKey:replacement];
    return replacement;
}
static void UUForgetOwnership(void) {
    UUOwned = nil; UUOwnedSource = nil; UUOwnedPixels = nil; UUHasSeed = NO;
}
static BOOL UUStillOwns(void) {
    @try {
        if (!UUOwned || !UUHasSeed || !UUSeed || !UUForeground()) return NO;
        int32_t seed = UUSeed();
        if (seed != UUOwnedSeed) return NO;
        NSCursor *current = UUCurrent();
        return current && NSEqualSizes(current.image.size, UUOwned.image.size) &&
            NSEqualPoints(current.hotSpot, UUOwned.hotSpot) && [UUPixels(current.image) isEqual:UUOwnedPixels] &&
            UUSeed() == seed;
    } @catch (NSException *exception) {
        (void)exception;
        return NO;
    }
}
static void UUEmit(NSCursor *source, NSCursor *replacement) {
    // This is the first and only call into the true setter for this event.
    UUOriginal(replacement, UUSetter);
    @try {
        UUOwned = replacement; UUOwnedSource = source;
        UUOwnedPixels = UUPixels(replacement.image);
        UUHasSeed = UUSeed != NULL;
        if (UUHasSeed) UUOwnedSeed = UUSeed();
    } @catch (NSException *exception) {
        (void)exception;
        UUForgetOwnership();
    }
}
static void UURestore(void) {
    NSCursor *source = UUOwnedSource;
    BOOL owned = UUStillOwns();
    UUForgetOwnership();
    if (owned && source) UUOriginal(source, UUSetter);
}
static void UUCancelExpiry(void) {
    if (UUExpiryTimer) { dispatch_source_cancel(UUExpiryTimer); UUExpiryTimer = nil; }
}
static void UUDisable(void) {
    BOOL changed = UUEnabled;
    UUEnabled = NO; UUExpires = 0; UUCancelExpiry(); UURestore();
    if (changed) UUReportClient(nil);
}
static void UUExpireIfNeeded(void) {
    if (UUEnabled && UUNow() >= UUExpires) UUDisable();
}
static void UUHook(NSCursor *cursor, SEL selector) {
    // AppKit cursor updates are supported on the main thread only.
    if (!NSThread.isMainThread) { UUOriginal(cursor, selector); return; }
    NSCursor *source = cursor, *replacement = nil;
    @try {
        if (UUEnabled && UUNow() >= UUExpires) {
            // This incoming event supplies the new cursor. Restoring the old
            // source first would issue two true setters for one event.
            UUEnabled = NO; UUExpires = 0; UUCancelExpiry(); UUForgetOwnership();
            UUReportClient(nil);
        }
        source = [UUSources objectForKey:cursor] ?: cursor;
        if (UUEnabled && UUScale > 1 && UUCustom(source)) replacement = UUScaled(source);
    } @catch (NSException *exception) {
        (void)exception;
        source = cursor; replacement = nil;
    }
    // Deliberately outside the conversion catch: never swallow or retry a
    // true system setter exception, and never issue a fallback second setter.
    if (replacement) { UUEmit(source, replacement); return; }
    UUForgetOwnership();
    UUOriginal(source, selector);
}
static void UURefreshOwned(void) {
    NSCursor *source = nil, *replacement = nil;
    @try {
        if (!UUStillOwns()) { UUForgetOwnership(); return; }
        source = UUOwnedSource;
        replacement = UUScaled(source);
    } @catch (NSException *exception) {
        (void)exception;
        // A failed replacement does not change the currently owned image.
        // Retain its original so a subsequent disable can still restore it.
        return;
    }
    if (replacement) UUEmit(source, replacement);
}
static void UUTryCurrentSource(void) {
    // currentCursor is this process's original stack cursor. Never promote a
    // system snapshot or one of our generated replacements into a new source.
    NSCursor *source = nil, *replacement = nil;
    @try {
        if (UUOwned || !UUSeed || !UUForeground()) return;
        int32_t seed = UUSeed();
        source = UUAppCurrent();
        if (!source || [UUSources objectForKey:source] || !UUCustom(source)) return;
        NSCursor *system = UUCurrent();
        NSString *pixels = UUPixels(source.image);
        if (!pixels || !system || !NSEqualSizes(source.image.size, system.image.size) ||
            !NSEqualPoints(source.hotSpot, system.hotSpot) || ![pixels isEqual:UUPixels(system.image)] ||
            UUSeed() != seed) return;
        replacement = UUScaled(source);
        if (!replacement || !UUForeground() || UUSeed() != seed) return;
    } @catch (NSException *exception) {
        (void)exception;
        return;
    }
    UUEmit(source, replacement);
}

int UUOfficeCursorSetScale(double scale) {
    if (!NSThread.isMainThread || !UUInstalled) return -2;
    if (UUConflict) return -5;
    if (!UUValidScale(scale)) return -4;
    UUScale = scale;
    UUExpireIfNeeded();
    if (scale == 1) UUDisable();
    else if (UUEnabled) UURefreshOwned();
    return 0;
}
static int UUReceive(NSString *json) {
    if (!NSThread.isMainThread || !UUInstalled) return -2;
    if (UUConflict) return -5;
    if (![json isKindOfClass:NSString.class]) return -4;
    NSData *data = [json dataUsingEncoding:NSUTF8StringEncoding];
    if (data.length > 4096) return -4;
    id value = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    if (![value isKindOfClass:NSDictionary.class]) return -4;
    NSDictionary *profile = value;
    id enabled = profile[@"enabled"], scale = profile[@"scale"], expires = profile[@"expires"];
    if (![enabled isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)enabled) != CFBooleanGetTypeID() ||
        ![scale isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)scale) == CFBooleanGetTypeID() ||
        ![expires isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)expires) == CFBooleanGetTypeID()) return -4;
    double requested = [scale doubleValue], deadline = [expires doubleValue], now = UUNow();
    if (!UUValidScale(requested) || !isfinite(deadline) || deadline <= now || deadline - now > 90) return -4;
    UUExpireIfNeeded();
    if (![enabled boolValue] || requested == 1) { UUScale = requested; UUDisable(); return 0; }
    BOOL changed = !UUEnabled || UUScale != requested;
    UUScale = requested; UUExpires = deadline; UUEnabled = YES;
    UUCancelExpiry();
    // Single deadline event; the hook independently checks time before every set.
    UUExpiryTimer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
    dispatch_source_set_timer(UUExpiryTimer, dispatch_walltime(NULL, (int64_t)((deadline - now) * NSEC_PER_SEC)),
                              DISPATCH_TIME_FOREVER, 0);
    dispatch_source_set_event_handler(UUExpiryTimer, ^{ UUExpireIfNeeded(); });
    dispatch_resume(UUExpiryTimer);
    UURefreshOwned();
    UUTryCurrentSource();
    if (changed) UUReportClient(nil);
    return 0;
}
static NSDictionary *UUClientPacket(NSString *request) {
    Dl_info info = {0};
    dladdr((const void *)&UUOfficeCursorInstall, &info);
    NSString *path = info.dli_fname ? [NSString stringWithUTF8String:info.dli_fname] : @"";
    BOOL live = UUEnabled && UUNow() < UUExpires;
    return @{ @"protocol": @2, @"module_version": @2,
        @"host": NSBundle.mainBundle.bundleIdentifier ?: @"", @"pid": @(getpid()),
        @"installed": UUInstalled ? @YES : @NO, @"enabled": live ? @YES : @NO,
        @"conflict": UUConflict ? @YES : @NO, @"scale": @(UUScale),
        @"timestamp": @(UUNow()), @"library_path": path ?: @"",
        @"request_id": request ?: @"", @"error": UUModuleError ?: @"" };
}
static void UUReportClient(NSString *request) {
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestHost) {
        if (!UUTestReports) UUTestReports = [NSMutableArray new];
        [UUTestReports addObject:UUClientPacket(request)];
        return;
    }
#endif
    NSData *data = [NSJSONSerialization dataWithJSONObject:UUClientPacket(request) options:0 error:NULL];
    if (!data) return;
    [NSDistributedNotificationCenter.defaultCenter postNotificationName:UUClientStatus
        object:[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]
        userInfo:nil options:NSNotificationDeliverImmediately];
}
@interface UUOfficeCursorObserver : NSObject
- (void)profile:(NSNotification *)note;
- (void)probe:(NSNotification *)note;
@end
@implementation UUOfficeCursorObserver
- (void)profile:(NSNotification *)note { UUReceive(note.object); }
- (void)probe:(NSNotification *)note {
    if (!NSThread.isMainThread || UUObserver != self) return;
    if ([note.object isKindOfClass:NSString.class] && [(NSString *)note.object length] <= 128)
        UUReportClient(note.object);
}
@end

// Failure diagnostics remain discoverable until an explicit Stop. Profiles are
// subscribed separately only after the hook is installed successfully.
static void UUEnsureProbeObserver(void) {
    if (UUObserver) return;
    UUObserver = [UUOfficeCursorObserver new];
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestHost) return;
#endif
    [NSDistributedNotificationCenter.defaultCenter addObserver:UUObserver selector:@selector(probe:)
        name:UUClientProbe object:nil suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
}
static void UURemoveObservers(void) {
#ifdef UU_OFFICE_CURSOR_TESTING
    if (!UUTestHost)
#endif
    if (UUObserver) [NSDistributedNotificationCenter.defaultCenter removeObserver:UUObserver];
    UUObserver = nil; UUProfileObserving = NO;
}
static int UUHookConflict(void) {
    UUConflict = YES; UUModuleError = @"hook_conflict";
    // Ownership changed: do not restore through the saved setter or alter the foreign hook.
    UUEnabled = NO; UUExpires = 0; UUCancelExpiry(); UUForgetOwnership();
    if (UUProfileObserving) {
#ifdef UU_OFFICE_CURSOR_TESTING
        if (!UUTestHost)
#endif
        [NSDistributedNotificationCenter.defaultCenter removeObserver:UUObserver name:UUNotification object:nil];
        UUProfileObserving = NO;
    }
    UUReportClient(nil);
    return -5;
}

int UUOfficeCursorInstall(void) {
    if (!NSThread.isMainThread) return -2;
    if (UUInstalled) {
        if (method_getImplementation(class_getInstanceMethod(UUTarget, UUSetter)) != (IMP)UUHook || UUConflict) return UUHookConflict();
        return 0;
    }
    BOOL allowed = UUAllowedHost(NSBundle.mainBundle.bundleIdentifier);
#ifdef UU_OFFICE_CURSOR_TESTING
    allowed = allowed || UUTestHost;
#endif
    if (!allowed) return -1;
    UUEnsureProbeObserver();
    UUModuleError = nil; UUConflict = NO;
    UUTarget = NSCursor.class;
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestHost) UUTarget = UUTestClass;
#endif
    UUSetter = NSSelectorFromString(@"_reallySet");
    Method method = class_getInstanceMethod(UUTarget, UUSetter);
    char returnType[32] = {0};
    if (method) method_getReturnType(method, returnType, sizeof(returnType));
    if (!method || method_getNumberOfArguments(method) != 2 || strcmp(returnType, @encode(void)) != 0 ||
        !class_getInstanceMethod(UUTarget, NSSelectorFromString(@"_coreCursorType"))) {
        UUModuleError = @"unsupported_runtime"; UUReportClient(nil); return -3;
    }
#ifndef UU_OFFICE_CURSOR_TESTING
    // A second copy must not wrap an existing add-in's setter and compound scale.
    Dl_info setterInfo = {0};
    IMP setter = method_getImplementation(method);
    NSString *setterPath = dladdr((const void *)setter, &setterInfo) && setterInfo.dli_fname
        ? [NSString stringWithUTF8String:setterInfo.dli_fname] : nil;
    if (![setterPath hasPrefix:@"/System/Library/Frameworks/AppKit.framework/"]) {
        return UUHookConflict();
    }
#else
    if (UUTestHost && UUTestRejectSetter) return UUHookConflict();
#endif
    UUOriginal = (void (*)(id, SEL))method_getImplementation(method);
#ifndef UU_OFFICE_CURSOR_TESTING
    void *library = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY | RTLD_LOCAL);
    UUSeed = library ? (int32_t (*)(void))dlsym(library, "SLSCurrentCursorSeed") : NULL;
#endif
    if (!UUSeed) { UUModuleError = @"cursor_seed_unavailable"; UUReportClient(nil); return -3; }
    UUCache = [NSMutableDictionary new]; UUSources = [NSMapTable weakToStrongObjectsMapTable];
    UUScale = 3; UUEnabled = NO; UUExpires = 0;
    class_replaceMethod(UUTarget, UUSetter, (IMP)UUHook, method_getTypeEncoding(method));
    UUInstalled = YES;
    UUProfileObserving = YES;
#ifdef UU_OFFICE_CURSOR_TESTING
    if (!UUTestHost)
#endif
    [NSDistributedNotificationCenter.defaultCenter addObserver:UUObserver selector:@selector(profile:)
        name:UUNotification object:nil suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
#ifdef UU_OFFICE_CURSOR_TESTING
    if (UUTestHost) UUTestRequests++;
    else
#endif
    [NSDistributedNotificationCenter.defaultCenter
        postNotificationName:@"local.codex.UUDisplayAutomation.OfficeCursorProfileRequest"
        object:nil userInfo:nil options:NSNotificationDeliverImmediately];
    UUReportClient(nil);
    return 0;
}
// Read-only health check for the dedicated add-in's diagnostic macro.
// 0 unloaded, 1 installed/idle, 3 installed/remote enabled, bit 4 hook conflict.
int UUOfficeCursorGetState(void) {
    if (!NSThread.isMainThread) return -2;
    BOOL live = UUEnabled && UUNow() < UUExpires;
    return (UUInstalled ? 1 : 0) | (live ? 2 : 0) | (UUConflict ? 4 : 0);
}
int UUOfficeCursorStop(void) {
    if (!NSThread.isMainThread) return -2;
    if (!UUInstalled) {
        BOOL hadObserver = UUObserver != nil;
        UUModuleError = nil; UUConflict = NO;
        if (hadObserver) UUReportClient(nil);
        UURemoveObservers();
        return 0;
    }
    Method method = class_getInstanceMethod(UUTarget, UUSetter);
    if (method_getImplementation(method) != (IMP)UUHook) return UUHookConflict();
    UUDisable();
    UURemoveObservers();
    class_replaceMethod(UUTarget, UUSetter, (IMP)UUOriginal, method_getTypeEncoding(method));
    UUInstalled = NO; UUConflict = NO; UUModuleError = nil; UUCache = nil; UUSources = nil;
    UUReportClient(nil);
    return 0;
}

#ifdef UU_OFFICE_CURSOR_TESTING
// Compiled out of the production library. The test class owns a stub setter.
int UUOfficeCursorTestHost(Class cls, NSCursor *(*current)(void), BOOL (*foreground)(void),
                          int32_t (*seed)(void), double (*now)(void)) {
    if (UUInstalled) return -2;
    UUTestHost = cls != Nil; UUTestClass = cls; UUTestCurrent = current;
    UUTestForeground = foreground; UUSeed = seed; UUTestNow = now;
    return 0;
}
int UUOfficeCursorTestReceive(NSString *json) { return UUReceive(json); }
void UUOfficeCursorTestExpire(void) { UUExpireIfNeeded(); }
BOOL UUOfficeCursorTestEnabled(void) { return UUEnabled; }
#endif
