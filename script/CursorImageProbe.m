// Local bounded diagnostic for the cursor registration used by Mousecape.
// API declarations checked against:
// https://github.com/seanjseymour/Mousecape/tree/macos27-cursor-names
// Default operation is read-only. Explicit backup/apply/restore operations only
// touch the bounded built-in cursor catalog, never display configuration,
// pointer position or input events.
#import <AppKit/AppKit.h>
#import <CommonCrypto/CommonDigest.h>
#import <dlfcn.h>

typedef int32_t (*ConnectionFn)(void);
typedef CGError (*CopyFn)(int32_t, const char *, CGSize *, CGPoint *, NSUInteger *, CGFloat *, CFArrayRef *);
typedef CGError (*RegisterFn)(int32_t, const char *, bool, bool, CGSize, CGPoint, NSUInteger, CGFloat, CFArrayRef, int *);
typedef CGError (*CoreCopyFn)(int32_t, int, CFArrayRef *, CGSize *, CGPoint *, NSUInteger *, CGFloat *);
static ConnectionFn connection;
static CopyFn copyCursor;
static RegisterFn registerCursor;
static CoreCopyFn coreCopyCursor;
static NSString *const arrowName = @"com.apple.coregraphics.ArrowS";
static NSArray<NSString *> *candidateNames(void) {
    NSMutableArray *names = [NSMutableArray arrayWithArray:@[@"ArrowS", @"IBeamS", @"IBeamXOR", @"Alias", @"Copy", @"Move", @"ArrowCtx", @"Wait", @"Empty"]];
    for (int i = 0; i <= 43; i++) [names addObject:[NSString stringWithFormat:@"Core%d", i]];
    return names;
}
static NSString *identifierForShortName(NSString *name) {
    if ([name hasPrefix:@"Core"]) return [@"com.apple.cursor." stringByAppendingString:[name substringFromIndex:4]];
    return [@"com.apple.coregraphics." stringByAppendingString:name];
}
static BOOL supportedName(NSString *name) {
    for (NSString *shortName in candidateNames()) if ([identifierForShortName(shortName) isEqual:name]) return YES;
    return NO;
}

static NSString *pixelHash(CGImageRef image) {
    size_t width = CGImageGetWidth(image), height = CGImageGetHeight(image);
    CGColorSpaceRef color = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(NULL, width, height, 8, width * 4, color,
                                                kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(color);
    if (!context) return @"unavailable";
    CGContextDrawImage(context, CGRectMake(0, 0, width, height), image);
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(CGBitmapContextGetData(context), (CC_LONG)(width * height * 4), digest);
    CGContextRelease(context);
    NSMutableString *hash = [NSMutableString string];
    for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) [hash appendFormat:@"%02x", digest[i]];
    return hash;
}

static NSDictionary *captureCursor(NSString *name) {
    CGSize size = CGSizeZero; CGPoint hotspot = CGPointZero;
    NSUInteger frames = 0; CGFloat duration = 0; CFArrayRef images = NULL;
    BOOL core = [name hasPrefix:@"com.apple.cursor."];
    CGError error = core ? (coreCopyCursor ? coreCopyCursor(connection(), name.pathExtension.intValue, &images, &size, &hotspot, &frames, &duration) : kCGErrorNotImplemented) :
        copyCursor(connection(), name.UTF8String, &size, &hotspot, &frames, &duration, &images);
    if (error || !images || CFArrayGetCount(images) == 0) {
        if (images) CFRelease(images);
        return nil;
    }
    NSMutableArray *pngs = [NSMutableArray array], *hashes = [NSMutableArray array];
    for (id object in (__bridge NSArray *)images) {
        if (CFGetTypeID((__bridge CFTypeRef)object) != CGImageGetTypeID()) { CFRelease(images); return nil; }
        CGImageRef image = (__bridge CGImageRef)object;
        NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithCGImage:image];
        NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        if (!png) { CFRelease(images); return nil; }
        [pngs addObject:png]; [hashes addObject:pixelHash(image)];
    }
    CFRelease(images);
    return @{@"identifier":name, @"width":@(size.width), @"height":@(size.height),
             @"hotspotX":@(hotspot.x), @"hotspotY":@(hotspot.y), @"frames":@(frames), @"duration":@(duration),
             @"pngs":pngs, @"pixelHashes":hashes};
}

static CGImageRef scaleSprite(CGImageRef largest, size_t width, size_t frameHeight, NSUInteger frames) {
    size_t height = frameHeight * frames;
    CGColorSpaceRef color = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(NULL, width, height, 8, width * 4, color,
                                                kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(color);
    if (!context) return NULL;
    CGContextSetInterpolationQuality(context, kCGInterpolationHigh);
    size_t sourceFrameHeight = CGImageGetHeight(largest) / frames;
    for (NSUInteger frame = 0; frame < frames; frame++) {
        CGImageRef tile = CGImageCreateWithImageInRect(largest,
            CGRectMake(0, frame * sourceFrameHeight, CGImageGetWidth(largest), sourceFrameHeight));
        if (!tile) { CGContextRelease(context); return NULL; }
        CGContextDrawImage(context, CGRectMake(0, (frames - 1 - frame) * frameHeight, width, frameHeight), tile);
        CGImageRelease(tile);
    }
    CGImageRef image = CGBitmapContextCreateImage(context);
    CGContextRelease(context); return image;
}

static int spriteSelfTest(void) {
    CGColorSpaceRef color = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef sourceContext = CGBitmapContextCreate(NULL, 4, 12, 8, 16, color,
        kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(color);
    if (!sourceContext) return 4;
    unsigned char *sourceBytes = CGBitmapContextGetData(sourceContext);
    for (int y=0;y<12;y++) for (int x=0;x<4;x++) {
        unsigned char *pixel = sourceBytes+(y*4+x)*4;
        pixel[y/4]=255;pixel[3]=255;
    }
    CGImageRef sourceImage = CGBitmapContextCreateImage(sourceContext);
    CGContextRelease(sourceContext);
    NSBitmapImageRep *source = [[NSBitmapImageRep alloc] initWithCGImage:sourceImage];CGImageRelease(sourceImage);
    NSUInteger checked=0;
    for (NSNumber *scale in @[@1.5,@2,@2.5,@3,@3.5,@4]) {
        size_t side = llround(4 * scale.doubleValue);
        CGImageRef image = scaleSprite(source.CGImage, side, side, 3);
        if (!image) return 4;
        NSBitmapImageRep *result = [[NSBitmapImageRep alloc] initWithCGImage:image];CGImageRelease(image);
        if (result.pixelsHigh != side*3 || result.pixelsWide != side) return 4;
        checked++;
        for (int frame=0;frame<3;frame++) {
            NSColor *actual = [[result colorAtX:side/2 y:frame*side+side/2] colorUsingColorSpace:NSColorSpace.sRGBColorSpace];
            NSColor *expected = [[source colorAtX:2 y:frame*4+2] colorUsingColorSpace:NSColorSpace.sRGBColorSpace];
            if (!actual || !expected || fabs(actual.redComponent-expected.redComponent)>.02 ||
                fabs(actual.greenComponent-expected.greenComponent)>.02 ||
                fabs(actual.blueComponent-expected.blueComponent)>.02) {
                fprintf(stderr,"sprite_order_failed scale=%.1f frame=%d\n",scale.doubleValue,frame);return 4;
            }
            checked++;
        }
    }
    printf("sprite_frame_order_and_dimensions_checks=%lu passed=1\n", (unsigned long)checked); return 0;
}

static int applyRecord(NSDictionary *record, double scale) {
    NSString *name = record[@"identifier"];
    NSUInteger frames = [record[@"frames"] unsignedIntegerValue];
    double duration = [record[@"duration"] doubleValue];
    if (!supportedName(name) || frames < 1 || frames > 64 || !isfinite(duration) || duration < 0 || duration > 10 ||
        !isfinite(scale) || scale < 1 || scale > 4 || fabs(scale * 2 - round(scale * 2)) > .0001) return 3;
    if (scale == 1) {
        NSDictionary *existing = captureCursor(name);
        BOOL identical = existing && [existing[@"pixelHashes"] isEqual:record[@"pixelHashes"]];
        for (NSString *field in @[@"width", @"height", @"hotspotX", @"hotspotY", @"frames", @"duration"])
            identical = identical && fabs([existing[field] doubleValue] - [record[field] doubleValue]) < .00001;
        if (identical) { puts("original_geometry_frames_duration_pixels_verified=1 unchanged=1 scale=1.00"); return 0; }
    }
    // The current registration API rejects >24 frames (the built-in wait ball
    // has 30). Do not mutate a cursor whose exact animation cannot be restored.
    if (frames > 24) { fputs("unsupported_frame_count_original_not_modified\n", stderr); return 5; }
    NSArray *pngs = record[@"pngs"];
    if (pngs.count < 1 || pngs.count > 8) return 3;
    NSMutableArray *reps = [NSMutableArray array], *images = [NSMutableArray array];
    CGImageRef largest = NULL;
    for (NSData *data in pngs) {
        NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithData:data];
        if (!rep.CGImage || rep.pixelsWide <= 0 || rep.pixelsHigh <= 0 ||
            rep.pixelsWide > 4096 || rep.pixelsHigh > 32768 ||
            rep.pixelsHigh % frames || (uint64_t)rep.pixelsWide * rep.pixelsHigh > 64000000) return 3;
        [reps addObject:rep];
        if (!largest || CGImageGetWidth(rep.CGImage) > CGImageGetWidth(largest)) largest = rep.CGImage;
    }
    for (NSBitmapImageRep *rep in reps) {
        if (scale == 1) { [images addObject:(__bridge id)rep.CGImage]; continue; }
        size_t width = (size_t)llround(rep.pixelsWide * scale);
        size_t frameHeight = (size_t)llround((rep.pixelsHigh / frames) * scale);
        size_t height = frameHeight * frames;
        if (!width || !frameHeight || width > 8192 || height > 131072 || (uint64_t)width * height > 64000000) return 3;
        CGImageRef image = scaleSprite(largest, width, frameHeight, frames);
        if (!image) return 3;
        [images addObject:CFBridgingRelease(image)];
    }
    CGSize size = CGSizeMake([record[@"width"] doubleValue] * scale, [record[@"height"] doubleValue] * scale);
    CGPoint hotspot = CGPointMake([record[@"hotspotX"] doubleValue] * scale, [record[@"hotspotY"] doubleValue] * scale);
    if (!isfinite(size.width) || !isfinite(size.height) || !isfinite(hotspot.x) || !isfinite(hotspot.y) ||
        size.width <= 0 || size.height <= 0 || size.width > 512 || size.height > 512 ||
        hotspot.x < 0 || hotspot.y < 0 || hotspot.x > size.width || hotspot.y > size.height) return 3;
    int seed = 0;
    CGError error = registerCursor(connection(), name.UTF8String, true, true, size, hotspot,
                                  frames, duration, (__bridge CFArrayRef)images, &seed);
    NSDictionary *current = captureCursor(name);
    BOOL geometry = current && fabs([current[@"width"] doubleValue] - size.width) < .01 &&
        fabs([current[@"height"] doubleValue] - size.height) < .01 &&
        fabs([current[@"hotspotX"] doubleValue] - hotspot.x) < .01 &&
        fabs([current[@"hotspotY"] doubleValue] - hotspot.y) < .01 &&
        [current[@"frames"] unsignedIntegerValue] == frames &&
        fabs([current[@"duration"] doubleValue] - duration) < .00001;
    NSMutableArray *expectedHashes = [NSMutableArray array];
    for (id image in images) [expectedHashes addObject:pixelHash((__bridge CGImageRef)image)];
    BOOL pixels = [current[@"pixelHashes"] isEqual:expectedHashes];
    BOOL originalPixels = scale != 1 || [current[@"pixelHashes"] isEqual:record[@"pixelHashes"]];
    printf("register_error=%d geometry_verified=%d pixels_verified=%d original_pixels_verified=%d scale=%.2f\n",
           error, geometry, pixels, originalPixels, scale);
    return error == 0 && geometry && pixels && originalPixels ? 0 : 4;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        void *sky = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY | RTLD_LOCAL);
        connection = (ConnectionFn)dlsym(sky, "CGSMainConnectionID");
        if (!connection) connection = (ConnectionFn)dlsym(sky, "SLSMainConnectionID");
        copyCursor = (CopyFn)dlsym(sky, "CGSCopyRegisteredCursorImages");
        if (!copyCursor) copyCursor = (CopyFn)dlsym(sky, "SLSCopyRegisteredCursorImages");
        registerCursor = (RegisterFn)dlsym(sky, "CGSRegisterCursorWithImages");
        if (!registerCursor) registerCursor = (RegisterFn)dlsym(sky, "SLSRegisterCursorWithImages");
        coreCopyCursor = (CoreCopyFn)dlsym(RTLD_DEFAULT, "CoreCursorCopyImages");
        if (!connection || !copyCursor) { fprintf(stderr, "Cursor inspection API unavailable\n"); return 2; }
        if (argc == 2 && strcmp(argv[1], "--self-test") == 0) return spriteSelfTest();
        if (argc == 2 && strcmp(argv[1], "--list") == 0) {
            NSMutableArray *list = [NSMutableArray array];
            for (NSString *shortName in candidateNames()) {
                @autoreleasepool {
                    NSString *identifier = identifierForShortName(shortName);
                    NSDictionary *record = captureCursor(identifier);
                    // Core0/1 draw from ArrowS/IBeamS; applying both aliases
                    // would duplicate their mutation and recovery records.
                    if ([shortName isEqual:@"Core0"] || [shortName isEqual:@"Core1"]) continue;
                    if (!record || [record[@"frames"] unsignedIntegerValue] < 1 ||
                        [record[@"frames"] unsignedIntegerValue] > 24) continue;
                    [list addObject:@{@"name":shortName, @"identifier":identifier,
                        @"frames":record[@"frames"], @"duration":record[@"duration"],
                        @"width":record[@"width"], @"height":record[@"height"],
                        @"imageCount":@([record[@"pngs"] count])}];
                }
            }
            NSData *data = [NSJSONSerialization dataWithJSONObject:list options:0 error:nil];
            fwrite(data.bytes, 1, data.length, stdout); puts(""); return 0;
        }
        if ((argc == 3 || argc == 4) && strcmp(argv[1], "--backup") == 0) {
            NSString *name = argc == 4 ? @(argv[3]) : arrowName;
            if (!supportedName(name)) return 3;
            NSDictionary *record = captureCursor(name);
            if (!record) return 3;
            NSError *error = nil;
            NSData *data = [NSPropertyListSerialization dataWithPropertyList:record format:NSPropertyListBinaryFormat_v1_0 options:0 error:&error];
            BOOL saved = [data writeToFile:@(argv[2]) options:NSDataWritingWithoutOverwriting error:&error];
            if (!saved) { fprintf(stderr, "%s\n", error.localizedDescription.UTF8String); return 3; }
            puts("Original cursor images and pixel hashes backed up"); return 0;
        }
        if (argc >= 3 && (strcmp(argv[1], "--apply") == 0 || strcmp(argv[1], "--restore") == 0)) {
            if (!registerCursor) return 2;
            NSData *data = [NSData dataWithContentsOfFile:@(argv[2])];
            NSDictionary *record = data ? [NSPropertyListSerialization propertyListWithData:data options:NSPropertyListImmutable format:NULL error:nil] : nil;
            if (!record) return 3;
            double scale = strcmp(argv[1], "--restore") == 0 ? 1 : (argc == 4 ? atof(argv[3]) : 2);
            return applyRecord(record, scale);
        }
        if (argc != 1) return 2;
        NSMutableArray *result = [NSMutableArray array];
        NSCursor *currentCursor = NSCursor.currentSystemCursor;
        if (currentCursor.image) {
            NSMutableArray *variants = [NSMutableArray array];
            for (NSImageRep *rep in currentCursor.image.representations)
                [variants addObject:@{@"width":@(rep.pixelsWide), @"height":@(rep.pixelsHigh)}];
            [result addObject:@{@"name":@"NSCursor.currentSystemCursor", @"pointsWide":@(currentCursor.image.size.width),
                                @"pointsHigh":@(currentCursor.image.size.height), @"hotspotX":@(currentCursor.hotSpot.x),
                                @"hotspotY":@(currentCursor.hotSpot.y), @"variants":variants}];
        }
        for (NSString *name in @[@"com.apple.coregraphics.ArrowS", @"com.apple.coregraphics.Arrow", @"com.apple.coregraphics.IBeamS"]) {
            CGSize size = CGSizeZero; CGPoint hotspot = CGPointZero;
            NSUInteger frames = 0; CGFloat duration = 0; CFArrayRef images = NULL;
            CGError error = copyCursor(connection(), name.UTF8String, &size, &hotspot, &frames, &duration, &images);
            NSMutableArray *variants = [NSMutableArray array];
            if (images) {
                for (id object in (__bridge NSArray *)images) {
                    if (CFGetTypeID((__bridge CFTypeRef)object) != CGImageGetTypeID()) continue;
                    CGImageRef image = (__bridge CGImageRef)object;
                    [variants addObject:@{@"width": @(CGImageGetWidth(image)), @"height": @(CGImageGetHeight(image))}];
                }
                CFRelease(images);
            }
            [result addObject:@{@"name":name, @"error":@(error), @"pointsWide":@(size.width), @"pointsHigh":@(size.height),
                                @"hotspotX":@(hotspot.x), @"hotspotY":@(hotspot.y), @"frames":@(frames), @"duration":@(duration), @"variants":variants}];
        }
        NSData *json = [NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingPrettyPrinted error:nil];
        fwrite(json.bytes, 1, json.length, stdout); fputc('\n', stdout);
    }
    return 0;
}
