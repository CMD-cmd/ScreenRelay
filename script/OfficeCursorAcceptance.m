#import <AppKit/AppKit.h>
#import <CommonCrypto/CommonDigest.h>
#import <dlfcn.h>

// Bounded, read-only cursor measurement, or an explicitly requested short test
// lease. No mouse events, window contents, workbook APIs, or persistent settings.
int main(int argc, const char **argv) { @autoreleasepool {
    if (argc != 4) return 2;
    double amount = atof(argv[2]), seconds = atof(argv[3]);
    if (!isfinite(seconds) || seconds <= 0 || seconds > 60) return 2;
    if (!strcmp(argv[1], "--lease") || !strcmp(argv[1], "--packet")) {
        if (amount != 1 && amount != 3) return 2;
        NSDictionary *packet = @{ @"enabled": amount > 1 ? @YES : @NO, @"scale": @(amount),
            @"expires": @(NSDate.date.timeIntervalSince1970 + seconds) };
        NSData *data = [NSJSONSerialization dataWithJSONObject:packet options:0 error:NULL];
        NSString *object = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        puts(object.UTF8String);
        if (!strcmp(argv[1], "--packet")) return 0;
        [NSDistributedNotificationCenter.defaultCenter
            postNotificationName:@"local.codex.UUDisplayAutomation.OfficeCursorProfile"
            object:object
            userInfo:nil options:NSNotificationDeliverImmediately];
        return 0;
    }
    if (strcmp(argv[1], "--observe") || amount != 0) return 2;
    [NSApplication sharedApplication]; [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
    void *library = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY | RTLD_LOCAL);
    int32_t (*seed)(void) = library ? dlsym(library, "SLSCurrentCursorSeed") : NULL;
    if (!seed) return 3;
    __block int32_t previous = INT32_MIN;
    dispatch_source_t timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
    dispatch_source_set_timer(timer, DISPATCH_TIME_NOW, 10 * NSEC_PER_MSEC, NSEC_PER_MSEC);
    dispatch_source_set_event_handler(timer, ^{
        @autoreleasepool {
            int32_t current = seed(); if (current == previous) return; previous = current;
            NSCursor *cursor = NSCursor.currentSystemCursor;
            if (!cursor) return;
            NSData *pixels = cursor.image.TIFFRepresentation;
            unsigned char digest[CC_SHA256_DIGEST_LENGTH] = {0};
            if (pixels) CC_SHA256(pixels.bytes, (CC_LONG)pixels.length, digest);
            NSMutableString *hash = [NSMutableString new];
            for (NSUInteger i = 0; i < sizeof(digest); i++) [hash appendFormat:@"%02x", digest[i]];
            NSMutableArray *variants = [NSMutableArray new];
            for (NSImageRep *rep in cursor.image.representations)
                [variants addObject:@[@(rep.pixelsWide), @(rep.pixelsHigh)]];
            NSDictionary *row = @{ @"timestamp": @(NSDate.date.timeIntervalSince1970),
                @"seed": @(current), @"width": @(cursor.image.size.width), @"height": @(cursor.image.size.height),
                @"hotspot": @[@(cursor.hotSpot.x), @(cursor.hotSpot.y)], @"variants": variants,
                @"image_hash": hash,
                @"foreground": NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier ?: @"" };
            NSData *data = [NSJSONSerialization dataWithJSONObject:row options:0 error:NULL];
            puts([[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding].UTF8String); fflush(stdout);
        }
    }); dispatch_resume(timer);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, seconds * NSEC_PER_SEC), dispatch_get_main_queue(), ^{ exit(0); });
    [NSApp run]; dispatch_source_cancel(timer);
    return 0;
} }
