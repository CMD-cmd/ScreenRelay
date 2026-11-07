// clang -fobjc-arc -framework AppKit script/test_office_cursor_scaler.m -o /tmp/test-office-cursor-scaler
// Every setter invocation is a stub on a test-only subclass; no real cursor set.
#define UU_OFFICE_CURSOR_TESTING 1
#import "OfficeCursorScaler.m"
#import <objc/message.h>

static NSMutableArray<NSCursor *> *Calls;
static NSCursor *Current;
static NSCursor *OriginalStack;
static int32_t Seed;
static BOOL Foreground = YES;
static double Now = 1000;
static NSUInteger Checks;
static BOOL ThrowSeedRead, ThrowSeedAfterSetter, ThrowTrueSetter;
static void Check(BOOL condition, NSString *message) {
    Checks++;
    if (!condition) { fprintf(stderr, "FAIL: %s\n", message.UTF8String); exit(1); }
}
static NSCursor *CurrentStub(void) { return Current; }
static NSCursor *AppCurrentStub(void) { return OriginalStack; }
static BOOL ForegroundStub(void) { return Foreground; }
static int32_t SeedStub(void) {
    if (ThrowSeedRead) @throw [NSException exceptionWithName:@"SeedProbeFailure" reason:nil userInfo:nil];
    return Seed;
}
static double NowStub(void) { return Now; }
static void TrueSetterStub(id cursor, SEL selector) {
    Check(selector == NSSelectorFromString(@"_reallySet"), @"saved IMP receives the original selector");
    [Calls addObject:cursor]; Current = cursor; Seed++;
    if (ThrowSeedAfterSetter) ThrowSeedRead = YES;
    if (ThrowTrueSetter) @throw [NSException exceptionWithName:@"TrueSetterFailure" reason:nil userInfo:nil];
}
@interface UUTestCursor : NSCursor
- (void)_reallySet;
- (NSInteger)_coreCursorType;
@end
@implementation UUTestCursor
- (void)_reallySet { TrueSetterStub(self, _cmd); }
- (NSInteger)_coreCursorType { return NSIntegerMax; }
@end
@interface UUTestBuiltinCursor : UUTestCursor
@end
@implementation UUTestBuiltinCursor
- (NSInteger)_coreCursorType { return 0; }
@end
@interface UUMissingSetterHost : NSObject
@end
@implementation UUMissingSetterHost
@end
@interface UUThrowCursor : UUTestCursor
@property BOOL failCore, failImage;
@end
@implementation UUThrowCursor
- (NSInteger)_coreCursorType {
    if (self.failCore) @throw [NSException exceptionWithName:@"CoreGetterFailure" reason:nil userInfo:nil];
    return [super _coreCursorType];
}
- (NSImage *)image {
    if (self.failImage) @throw [NSException exceptionWithName:@"ImageGetterFailure" reason:nil userInfo:nil];
    return [super image];
}
@end
@interface UUThrowImage : NSImage
@property BOOL failTIFF;
@end
@implementation UUThrowImage
- (NSData *)TIFFRepresentation {
    if (self.failTIFF) @throw [NSException exceptionWithName:@"TIFFFailure" reason:nil userInfo:nil];
    return [super TIFFRepresentation];
}
@end
@interface UUThrowBitmap : NSBitmapImageRep
@property BOOL failDraw;
@property NSUInteger drawAttempts;
@end
@implementation UUThrowBitmap
- (BOOL)drawInRect:(NSRect)rect {
    self.drawAttempts++;
    if (self.failDraw) @throw [NSException exceptionWithName:@"DrawingFailure" reason:nil userInfo:nil];
    return [super drawInRect:rect];
}
@end

static void Set(NSCursor *cursor) {
    OriginalStack = cursor;
    ((void (*)(id, SEL))objc_msgSend)(cursor, NSSelectorFromString(@"_reallySet"));
}
static NSCursor *Make(Class cls, NSInteger variant) {
    NSImage *image = [[NSImage alloc] initWithSize:NSMakeSize(12, 10)];
    for (NSInteger density = 1; density <= 2; density++) {
        NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL
            pixelsWide:12*density pixelsHigh:10*density bitsPerSample:8 samplesPerPixel:4
            hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
        memset(rep.bitmapData, 0, rep.bytesPerRow * rep.pixelsHigh);
        for (NSInteger y = 0; y < rep.pixelsHigh; y++) for (NSInteger x = 0; x < rep.pixelsWide; x++) {
            unsigned char *pixel = rep.bitmapData + y * rep.bytesPerRow + x * 4;
            pixel[0] = (x * 17 + variant) % 255; pixel[1] = (y * 13) % 255;
            pixel[2] = variant % 255; pixel[3] = 255;
        }
        rep.size = image.size; [image addRepresentation:rep];
    }
    return [[cls alloc] initWithImage:image hotSpot:NSMakePoint(2, 3)];
}
static NSString *Lease(BOOL enabled, double scale, double expires) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"enabled":@(enabled), @"scale":@(scale), @"expires":@(expires)}
        options:0 error:NULL];
    return [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
}
static void Enable(double scale) {
    Check(UUOfficeCursorTestReceive(Lease(YES, scale, Now + 60)) == 0, @"accept fresh supported lease");
}
static void Fresh(void) {
    ThrowSeedRead = NO; ThrowSeedAfterSetter = NO; ThrowTrueSetter = NO;
    Check(UUOfficeCursorStop() == 0, @"clean stop");
    [Calls removeAllObjects]; Current = nil; OriginalStack = nil; Foreground = YES;
    Check(UUOfficeCursorInstall() == 0, @"install test hook");
}
static void TestFirstSetterAndCache(void) {
    NSCursor *source = Make(UUTestCursor.class, 11);
    NSData *before = source.image.TIFFRepresentation;
    NSPoint originalHot = source.hotSpot;
    Set(source);
    Check(Calls.count == 1 && Calls.lastObject == source, @"remote disabled by default");
    [Calls removeAllObjects]; Enable(3);
    Check(Calls.count == 1 && Calls.lastObject.image.size.width == 36, @"enable immediately scales verified original stack source");
    [Calls removeAllObjects]; Set(source);
    NSCursor *large = Calls.lastObject;
    Check(Calls.count == 1 && large != source, @"first true setter only receives replacement");
    Check(NSEqualSizes(large.image.size, NSMakeSize(36, 30)), @"first true setter is already 3x");
    Check(NSEqualPoints(large.hotSpot, NSMakePoint(6, 9)), @"hotspot uses same factor");
    Check(large.image.representations.count == 2, @"both source and Retina representations retained");
    NSImageRep *rep1 = large.image.representations[0], *rep2 = large.image.representations[1];
    Check(rep1.pixelsWide == 36 && rep1.pixelsHigh == 30 && rep2.pixelsWide == 72 && rep2.pixelsHigh == 60,
          @"each representation pixel dimensions scaled");
    Check([before isEqual:source.image.TIFFRepresentation] && NSEqualPoints(originalHot, source.hotSpot) &&
          NSEqualSizes(source.image.size, NSMakeSize(12, 10)), @"original image, representations and hotspot unchanged");
    Set(source);
    Check(Calls.lastObject == large, @"same source reuses cached replacement");
    // Simulate a replacement re-entering the hook without invoking NSCursor.set.
    UUHook(large, NSSelectorFromString(@"_reallySet"));
    Check(Calls.lastObject == large && Calls.lastObject.image.size.width == 36, @"replacement never compounds");
    NSUInteger count = Calls.count;
    Check(UUOfficeCursorSetScale(2) == 0, @"scale can update owned cursor immediately");
    Check(Calls.count == count + 1 && Calls.lastObject.image.size.width == 24, @"3x to 2x uses original source");
    Check(UUOfficeCursorSetScale(3) == 0 && Calls.lastObject == large, @"scale change returns to same 3x cache");
    for (NSNumber *scale in @[@1.5, @2, @2.5, @3, @3.5, @4]) {
        Check(UUOfficeCursorSetScale(scale.doubleValue) == 0, @"all half-step scales supported");
        Check(Calls.lastObject.image.size.width == 12 * scale.doubleValue, @"supported scale remains absolute");
    }
    count = Calls.count;
    Check(UUOfficeCursorSetScale(1) == 0 && !UUOfficeCursorTestEnabled(), @"scale 1 disables");
    Check(Calls.count == count + 1 && Calls.lastObject == source, @"disable restores exact original object");
}
static void TestBuiltinAndOwnership(void) {
    Fresh(); Enable(3);
    NSCursor *builtin = Make(UUTestBuiltinCursor.class, 15);
    Set(builtin);
    Check(Calls.count == 1 && Calls.lastObject == builtin, @"built-in type passes through unchanged");
    NSCursor *source = Make(UUTestCursor.class, 17);
    Set(source); NSUInteger count = Calls.count;
    Foreground = NO;
    Check(UUOfficeCursorStop() == 0 && Calls.count == count, @"stop in another foreground app never restores");
    Fresh(); Enable(3); Set(source); count = Calls.count; Seed++;
    Check(UUOfficeCursorStop() == 0 && Calls.count == count, @"foreign seed prevents restore");
    Fresh(); Enable(3); Set(source); count = Calls.count;
    Current = Make(UUTestCursor.class, 99); // Even a coincidentally equal seed is insufficient.
    Check(UUOfficeCursorStop() == 0 && Calls.count == count, @"foreign image prevents restore");
    Fresh(); Enable(3); Set(source); count = Calls.count;
    Check(UUOfficeCursorStop() == 0 && Calls.count == count + 1 && Calls.lastObject == source,
          @"stop restores only owned current replacement");
    Set(source);
    Check(Calls.lastObject == source, @"stop reinstates saved original implementation");
}
static void TestLeases(void) {
    Fresh();
    for (NSString *bad in @[@"not json", @"[]", @"{}", @"{\"enabled\":1,\"scale\":3,\"expires\":1060}",
                            @"{\"enabled\":true,\"scale\":true,\"expires\":1060}",
                            @"{\"enabled\":true,\"scale\":3,\"expires\":true}",
                            Lease(YES, 3, Now), Lease(YES, 3, Now-1), Lease(YES, 3, Now+91),
                            Lease(YES, 2.25, Now+60), Lease(YES, 4.5, Now+60)]) {
        Check(UUOfficeCursorTestReceive(bad) == -4 && !UUOfficeCursorTestEnabled(), @"invalid lease cannot enable");
    }
    Check(UUOfficeCursorSetScale(NAN) == -4 && UUOfficeCursorSetScale(INFINITY) == -4 &&
          UUOfficeCursorSetScale(0) == -4, @"invalid exported scales rejected");
    Check(UUOfficeCursorTestReceive(Lease(YES, 3, Now+90)) == 0, @"90 second boundary accepted");
    NSCursor *source = Make(UUTestCursor.class, 1); Set(source);
    NSUInteger count = Calls.count;
    Now += 90; UUOfficeCursorTestExpire();
    Check(!UUOfficeCursorTestEnabled() && Calls.count == count + 1 && Calls.lastObject == source,
          @"expiry restores owned cursor without polling");
    Set(source);
    Check(Calls.lastObject == source, @"expired hook passes original through");
    Enable(3); Set(source); count = Calls.count;
    Check(UUOfficeCursorTestReceive(Lease(NO, 3, Now+60)) == 0 &&
          Calls.count == count+1 && Calls.lastObject == source, @"fresh explicit disable restores");
    Check(UUOfficeCursorTestReceive(Lease(YES, 1, Now+60)) == 0 && !UUOfficeCursorTestEnabled(), @"scale 1 lease disabled");
    Enable(3); Set(source); count = Calls.count;
    // A new lease replaces the old one-shot deadline.
    Now += 30; Enable(2); Now += 31; UUOfficeCursorTestExpire();
    Check(UUOfficeCursorTestEnabled() && Calls.count == count + 1, @"renewal invalidates old expiry");
}
static void TestTimerEvent(void) {
    Fresh();
    // Fake wall clock reaches expiry before the real one-shot wakes in 20ms.
    Check(UUOfficeCursorTestReceive(Lease(YES, 3, Now+0.02)) == 0, @"short fresh lease accepted");
    NSCursor *source = Make(UUTestCursor.class, 3); Set(source); Now += 1;
    NSDate *limit = [NSDate dateWithTimeIntervalSinceNow:0.3];
    while (UUOfficeCursorTestEnabled() && limit.timeIntervalSinceNow > 0)
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
    Check(!UUOfficeCursorTestEnabled() && Calls.lastObject == source, @"one-shot main-queue expiry performs restore");
}
static void TestImmediateSourceGuards(void) {
    Fresh();
    NSCursor *source = Make(UUTestCursor.class, 4);
    OriginalStack = source; Current = Make(UUTestCursor.class, 5);
    Enable(3);
    Check(Calls.count == 0, @"mismatched system pixels defer until normal setter");
    Set(source);
    NSCursor *generated = Calls.lastObject;
    Check(generated.image.size.width == 36, @"next ordinary setter is synchronously 3x");
    // Disown without removing the generated-to-source provenance map.
    UUForgetOwnership(); OriginalStack = generated; Current = generated;
    NSUInteger count = Calls.count; Enable(3);
    Check(Calls.count == count, @"generated stack candidate cannot become a compounded source");
    Fresh(); OriginalStack = source; Current = source; Foreground = NO;
    Enable(3); Check(Calls.count == 0, @"background lease does not immediately set a global cursor");
    Fresh(); NSCursor *builtin = Make(UUTestBuiltinCursor.class, 7);
    OriginalStack = builtin; Current = builtin;
    Enable(3); Check(Calls.count == 0, @"built-in current cursor excluded from immediate enable");
}
static void TestDeadlineHook(void) {
    Fresh(); Enable(3);
    NSCursor *old = Make(UUTestCursor.class, 25), *incoming = Make(UUTestCursor.class, 26);
    Set(old); [Calls removeAllObjects];
    Now = UUExpires;
    NSUInteger reports = UUTestReports.count;
    Set(incoming);
    Check(Calls.count == 1 && Calls.lastObject == incoming, @"at-deadline hook sets incoming source exactly once without old restore");
    Check(!UUOfficeCursorTestEnabled() && !UUExpiryTimer && !UUOwned, @"at-deadline hook disables, cancels and disowns");
    Check(UUTestReports.count == reports + 1 && ![UUTestReports.lastObject[@"enabled"] boolValue], @"deadline hook reports disabled state exactly once");
    UUOfficeCursorTestExpire();
    Check(Calls.count == 1 && UUTestReports.count == reports + 1, @"cancelled expiry helper causes no duplicate setter or report");
}
static void TestConversionExceptions(void) {
    Fresh(); Enable(3);
    UUThrowCursor *getter = (UUThrowCursor *)Make(UUThrowCursor.class, 30);
    NSData *original = getter.image.TIFFRepresentation;
    getter.failCore = YES; Set(getter);
    Check(Calls.count == 1 && Calls.lastObject == getter, @"throwing private getter passes original to true setter once");
    getter.failCore = NO; getter.failImage = YES; [Calls removeAllObjects]; Set(getter);
    Check(Calls.count == 1 && Calls.lastObject == getter, @"throwing image getter passes original to true setter once");
    getter.failImage = NO;
    Check([original isEqual:getter.image.TIFFRepresentation], @"getter failures preserve original image");

    Fresh(); Enable(3);
    NSImage *base = Make(UUTestCursor.class, 31).image;
    UUThrowImage *image = [[UUThrowImage alloc] initWithSize:base.size];
    for (NSImageRep *rep in base.representations) [image addRepresentation:[rep copy]];
    NSCursor *source = [[UUTestCursor alloc] initWithImage:image hotSpot:NSMakePoint(2, 3)];
    NSData *before = image.TIFFRepresentation; image.failTIFF = YES; Set(source);
    Check(Calls.count == 1 && Calls.lastObject == source, @"throwing TIFF conversion falls back exactly once");
    image.failTIFF = NO;
    Check([before isEqual:image.TIFFRepresentation] && NSEqualPoints(source.hotSpot, NSMakePoint(2,3)), @"TIFF failure preserves source and hotspot");

    Fresh(); Enable(3);
    UUThrowBitmap *rep = [[UUThrowBitmap alloc] initWithBitmapDataPlanes:NULL pixelsWide:12 pixelsHigh:10
        bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
    memset(rep.bitmapData, 127, rep.bytesPerRow * rep.pixelsHigh); rep.size = NSMakeSize(12,10);
    NSImage *drawImage = [[NSImage alloc] initWithSize:rep.size]; [drawImage addRepresentation:rep];
    NSCursor *drawSource = [[UUTestCursor alloc] initWithImage:drawImage hotSpot:NSMakePoint(2,3)];
    NSData *drawBefore = drawImage.TIFFRepresentation;
    NSGraphicsContext *prior = NSGraphicsContext.currentContext;
    rep.failDraw = YES; Set(drawSource);
    Check(rep.drawAttempts == 1 && Calls.count == 1 && Calls.lastObject == drawSource, @"throwing drawing falls back exactly once");
    Check(NSGraphicsContext.currentContext == prior, @"graphics state restored after drawing exception");
    rep.failDraw = NO;
    Check([drawBefore isEqual:drawImage.TIFFRepresentation], @"drawing failure preserves original pixels");

    Fresh(); Enable(3); Set(getter);
    NSUInteger count = Calls.count; getter.failImage = YES; Current = getter;
    Check(UUOfficeCursorStop() == 0 && Calls.count == count, @"restore validation exception safely declines ownership");
    getter.failImage = NO;
    Fresh(); Enable(3); ThrowSeedAfterSetter = YES; Set(getter);
    Check(Calls.count == 1 && Calls.lastObject != getter && !UUOwned, @"post-set ownership exception neither escapes nor calls fallback setter");
    Fresh(); Enable(3); ThrowTrueSetter = YES;
    BOOL escaped = NO;
    @try { Set(getter); } @catch (NSException *exception) { escaped = [exception.name isEqual:@"TrueSetterFailure"]; }
    Check(escaped && Calls.count == 1, @"true setter exception propagates without fallback retry");
    ThrowTrueSetter = NO;
}
static void ForeignSetterStub(id cursor, SEL selector) { TrueSetterStub(cursor, selector); }
static void TestFailedRefreshRetainsRestoration(void) {
    Fresh(); Enable(3);
    NSImage *base = Make(UUTestCursor.class, 71).image;
    UUThrowImage *image = [[UUThrowImage alloc] initWithSize:base.size];
    for (NSImageRep *rep in base.representations) [image addRepresentation:[rep copy]];
    NSCursor *source = [[UUTestCursor alloc] initWithImage:image hotSpot:NSMakePoint(2,3)];
    Set(source);
    Check(UUOwnedSource == source, @"original retained after successful set");
    NSUInteger count = Calls.count;
    image.failTIFF = YES;
    Enable(2);
    Check(Calls.count == count && UUOwnedSource == source, @"failed replacement retains existing restoration original");
    Check(UUOfficeCursorTestReceive(Lease(NO, 1, Now+60)) == 0 &&
          Calls.count == count+1 && Calls.lastObject == source, @"disable restores after failed refresh conversion");
    image.failTIFF = NO;
}
static void TestHookConflict(void) {
    Fresh(); Enable(3);
    Method method = class_getInstanceMethod(UUTestCursor.class, NSSelectorFromString(@"_reallySet"));
    class_replaceMethod(UUTestCursor.class, NSSelectorFromString(@"_reallySet"), (IMP)ForeignSetterStub, method_getTypeEncoding(method));
    NSUInteger conflictReports = UUTestReports.count;
    Check(UUOfficeCursorInstall() == -5 && [UUModuleError isEqual:@"hook_conflict"], @"installed idempotent branch discovers foreign hook and stable error");
    Check(UUTestReports.count == conflictReports + 1 && !UUEnabled && !UUProfileObserving && UUObserver,
        @"discovered foreign hook reports once, disables lease, keeps only probe observer");
    [(UUOfficeCursorObserver *)UUObserver probe:[NSNotification notificationWithName:UUClientProbe object:@"foreign-hook-refresh"]];
    Check([UUTestReports.lastObject[@"error"] isEqual:@"hook_conflict"]
        && [UUTestReports.lastObject[@"request_id"] isEqual:@"foreign-hook-refresh"], @"foreign-hook refresh retains error response");
    Check(UUOfficeCursorStop() == -5, @"foreign IMP conflict reported without overwriting it");
    NSUInteger requests = UUTestRequests;
    Check(UUOfficeCursorInstall() == -5 && UUObserver && UUTestRequests == requests, @"conflicting module keeps diagnostics without a new profile handshake");
    Check([UUModuleError isEqual:@"hook_conflict"] && [UUTestReports.lastObject[@"error"] isEqual:@"hook_conflict"], @"idempotent install conflict records and reports stable error");
    Check(UUOfficeCursorTestReceive(Lease(YES, 3, Now+60)) == -5 && !UUEnabled, @"conflict latch refuses stale notification enable");
    Check(method_getImplementation(class_getInstanceMethod(UUTestCursor.class, NSSelectorFromString(@"_reallySet"))) == (IMP)ForeignSetterStub,
          @"foreign hook remains intact");
    // Only test cleanup restores our intercepted stub, then performs a safe Stop.
    class_replaceMethod(UUTestCursor.class, NSSelectorFromString(@"_reallySet"), (IMP)UUHook, method_getTypeEncoding(method));
    Check(UUOfficeCursorStop() == 0, @"test cleanup can stop after restoring expected hook ownership");
}
static void Probe(NSString *nonce) {
    [(UUOfficeCursorObserver *)UUObserver probe:[NSNotification notificationWithName:UUClientProbe object:nonce]];
}
static void TestFailedInstallDiagnostics(void) {
    Check(UUOfficeCursorStop() == 0, @"stop before failed-install tests");
    for (NSString *error in @[@"unsupported_runtime", @"cursor_seed_unavailable", @"hook_conflict"]) {
        BOOL missingSetter = [error isEqual:@"unsupported_runtime"];
        BOOL missingSeed = [error isEqual:@"cursor_seed_unavailable"];
        UUTestRejectSetter = [error isEqual:@"hook_conflict"];
        Check(UUOfficeCursorTestHost(missingSetter ? UUMissingSetterHost.class : UUTestCursor.class,
            CurrentStub, ForegroundStub, missingSeed ? NULL : SeedStub, NowStub) == 0, @"configure isolated install failure");
        NSUInteger requests = UUTestRequests, reports = UUTestReports.count;
        int expected = UUTestRejectSetter ? -5 : -3;
        Check(UUOfficeCursorInstall() == expected, @"injected native dependency failure returns stable code");
        Check(UUObserver && !UUProfileObserving && !UUInstalled && UUTestRequests == requests,
            @"first install failure retains only read-only probe observer and no handshake");
        Check(UUTestReports.count == reports + 1 && [UUTestReports.lastObject[@"error"] isEqual:error]
            && ![UUTestReports.lastObject[@"installed"] boolValue], @"initial failure reports stable error and installed false");
        id observer = UUObserver;
        for (NSString *nonce in @[@"new-page-nonce", @"refresh-nonce"]) {
            NSUInteger count = UUTestReports.count;
            Probe(nonce);
            NSDictionary *packet = UUTestReports.lastObject;
            Check(UUTestReports.count == count + 1 && [packet[@"request_id"] isEqual:nonce]
                && [packet[@"error"] isEqual:error] && ![packet[@"installed"] boolValue]
                && ![packet[@"enabled"] boolValue], @"new page and refresh probes rediscover unchanged failed state");
        }
        NSUInteger count = UUTestReports.count;
        Probe([@"x" stringByPaddingToLength:129 withString:@"x" startingAtIndex:0]);
        Check(UUTestReports.count == count, @"oversized probe ignored");
        UUTestRejectSetter = NO;
        Check(UUOfficeCursorTestHost(UUTestCursor.class, CurrentStub, ForegroundStub, SeedStub, NowStub) == 0,
            @"repair native dependency without stopping failed module");
        Check(UUOfficeCursorInstall() == 0 && UUObserver == observer && UUProfileObserving && !UUModuleError && !UUConflict,
            @"retry success reuses diagnostic observer and clears previous error");
        Probe(@"recovered-nonce");
        Check([UUTestReports.lastObject[@"installed"] boolValue]
            && [UUTestReports.lastObject[@"error"] isEqual:@""], @"successful retry probe reports clean installed state");
        Check(UUOfficeCursorStop() == 0 && !UUObserver && !UUProfileObserving, @"successful stop releases both observers");
        count = UUTestReports.count;
        [(UUOfficeCursorObserver *)observer probe:[NSNotification notificationWithName:UUClientProbe object:@"stale-after-stop"]];
        Check(UUTestReports.count == count, @"queued old observer cannot reply after Stop");
    }
    Check(UUOfficeCursorTestHost(UUMissingSetterHost.class, CurrentStub, ForegroundStub, SeedStub, NowStub) == 0,
        @"configure failure for explicit stop lifecycle");
    Check(UUOfficeCursorInstall() == -3 && UUObserver, @"failed module has probe lifecycle");
    id failedObserver = UUObserver;
    Check(UUOfficeCursorStop() == 0 && !UUObserver && !UUModuleError && !UUConflict,
        @"explicit stop of never-installed module removes diagnostics and clears error");
    NSUInteger count = UUTestReports.count;
    [(UUOfficeCursorObserver *)failedObserver probe:[NSNotification notificationWithName:UUClientProbe object:@"stopped-failure"]];
    Check(UUTestReports.count == count, @"failed module stopped observer stays silent");
    Check(UUOfficeCursorTestHost(UUTestCursor.class, CurrentStub, ForegroundStub, SeedStub, NowStub) == 0,
        @"restore test host after failure tests");
}
int main(void) {
    @autoreleasepool {
        Calls = [NSMutableArray new];
        Check(UUAllowedHost(@"com.microsoft.Excel"), @"Excel remains an allowed host");
        Check(UUAllowedHost(@"com.microsoft.Powerpoint"), @"PowerPoint exact published bundle identifier allowed");
        for (id host in @[@"com.microsoft.Word", @"com.microsoft.PowerPoint", @"com.microsoft.Powerpoint.other", @"", NSNull.null])
            Check(!UUAllowedHost(host == NSNull.null ? nil : host), @"unverified host rejected without wildcard matching");
        Check(UUOfficeCursorGetState() == 0, @"unloaded diagnostic state");
        Check(UUOfficeCursorInstall() == -1, @"unapproved process denied without explicit test host");
        Check(UUOfficeCursorTestHost(UUMissingSetterHost.class, CurrentStub, ForegroundStub, SeedStub, NowStub) == 0,
              @"explicit test host selected");
        Check(UUOfficeCursorInstall() == -3, @"missing interception point fails without fallback");
        Check(UUOfficeCursorTestHost(UUTestCursor.class, CurrentStub, ForegroundStub, SeedStub, NowStub) == 0,
              @"test host uses stub true setter");
        UUTestAppCurrent = AppCurrentStub;
        Check(UUOfficeCursorInstall() == 0 && UUOfficeCursorInstall() == 0, @"installation idempotent");
        Check(UUOfficeCursorGetState() == 1, @"newly installed module starts idle");
        Check(UUTestRequests == 1, @"one handshake request per installation; test suppresses external notification");
        Check(UUCustom(Make(NSCursor.class, 9)), @"real image NSCursor identifies custom via read-only core type getter");
        Method realMethod = class_getInstanceMethod(NSCursor.class, NSSelectorFromString(@"_reallySet"));
        printf("Runtime NSCursor _reallySet: %s (read-only introspection)\n", realMethod ? method_getTypeEncoding(realMethod) : "missing");
        TestFirstSetterAndCache(); TestBuiltinAndOwnership(); TestLeases(); TestTimerEvent(); TestImmediateSourceGuards();
        TestDeadlineHook(); TestConversionExceptions(); TestFailedRefreshRetainsRestoration(); TestHookConflict(); TestFailedInstallDiagnostics();
        Check(UUOfficeCursorStop() == 0, @"final shutdown");
        Check(UUOfficeCursorGetState() == 0, @"unloaded diagnostic resets after shutdown");
        printf("PASS: %lu assertions; all true setters intercepted by test stub; no global cursor writes.\n", (unsigned long)Checks);
    }
    return 0;
}
