#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/module.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("CS492");
MODULE_DESCRIPTION("Minimal external kernel module example");

static int __init hello_init(void)
{
	pr_info("kernel_hello: loaded\n");
	return 0;
}

static void __exit hello_exit(void)
{
	pr_info("kernel_hello: unloaded\n");
}

module_init(hello_init);
module_exit(hello_exit);
