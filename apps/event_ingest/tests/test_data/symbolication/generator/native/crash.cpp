// A tiny, dependency-free program whose only purpose is its debug info: the
// symbolication parity tests resolve addresses inside these functions. It is
// never run. Namespaces give the symbols mangled names, so demangling is
// exercised on every object format.

static volatile int sink;

namespace app {
namespace config {

__attribute__((noinline)) int parse_value(int value)
{
    int doubled = value * 2;
    sink = doubled;
    return doubled + 1;
}

}  // namespace config

__attribute__((noinline)) void crash_here(int *target)
{
    int local = config::parse_value(sink);
    *target = local;
}

__attribute__((noinline)) void run_job(int job)
{
    if (job > 0) {
        crash_here(nullptr);
    }
    sink = job;
}

}  // namespace app

extern "C" int main()
{
    app::run_job(1);
    return 0;
}
