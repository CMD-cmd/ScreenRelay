// Read-only system cursor inventory used for acceptance evidence.
#import <AppKit/AppKit.h>
#import <dlfcn.h>
typedef int32_t (*Conn)(void);
typedef const char *(*Name)(int);
typedef CGError (*Copy)(int32_t,const char *,CGSize *,CGPoint *,NSUInteger *,CGFloat *,CFArrayRef *);
typedef CGError (*CoreCopy)(int32_t,int,CFArrayRef *,CGSize *,CGPoint *,NSUInteger *,CGFloat *);
int main(void) { @autoreleasepool {
    void *sky=dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",RTLD_LAZY|RTLD_LOCAL);
    Conn conn=(Conn)dlsym(sky,"CGSMainConnectionID");
    Name name=(Name)dlsym(sky,"CGSCursorNameForSystemCursor");
    Copy copy=(Copy)dlsym(sky,"CGSCopyRegisteredCursorImages");
    CoreCopy core=(CoreCopy)dlsym(RTLD_DEFAULT,"CoreCursorCopyImages");
    if (!conn||!copy) return 2;
    NSMutableArray *result=[NSMutableArray array];
    for (int i=0;i<44;i++) {
        CGSize s=CGSizeZero; CGPoint h=CGPointZero; NSUInteger f=0; CGFloat d=0; CFArrayRef imgs=NULL;
        const char *n=name?name(i):NULL; NSString *identifier=n?@(n):[NSString stringWithFormat:@"com.apple.cursor.%d",i];
        CGError err=copy(conn(),identifier.UTF8String,&s,&h,&f,&d,&imgs);
        NSMutableDictionary *row=[@{@"id":@(i),@"identifier":identifier,@"registeredError":@(err)} mutableCopy];
        if ((err||!imgs)&&core) {if(imgs)CFRelease(imgs);imgs=NULL; err=core(conn(),i,&imgs,&s,&h,&f,&d);row[@"coreError"]=@(err);}
        NSMutableArray *sizes=[NSMutableArray array];
        if(imgs) {for(id o in (__bridge NSArray*)imgs) if(CFGetTypeID((__bridge CFTypeRef)o)==CGImageGetTypeID()) {CGImageRef x=(__bridge CGImageRef)o;[sizes addObject:@[@(CGImageGetWidth(x)),@(CGImageGetHeight(x))]];} CFRelease(imgs);}
        [row addEntriesFromDictionary:@{@"width":@(s.width),@"height":@(s.height),@"frames":@(f),@"duration":@(d),@"images":sizes}];[result addObject:row];
    }
    NSData *data=[NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingPrettyPrinted error:nil];fwrite(data.bytes,1,data.length,stdout);puts("");
} return 0; }
