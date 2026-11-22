// 将部署监督进程及其所有子进程绑定到退出时统一回收的 Windows 作业对象。
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;

public static class TaigerProcessJob
{
    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimits
    {
        public long ProcessTime;
        public long JobTime;
        public uint Flags;
        public UIntPtr MinimumWorkingSet;
        public UIntPtr MaximumWorkingSet;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperations;
        public ulong WriteOperations;
        public ulong OtherOperations;
        public ulong ReadBytes;
        public ulong WriteBytes;
        public ulong OtherBytes;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimits
    {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory;
        public UIntPtr JobMemory;
        public UIntPtr PeakProcessMemory;
        public UIntPtr PeakJobMemory;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    /// <summary>在生成子进程前把当前进程加入作业，父进程退出后内核回收整棵进程树。</summary>
    /// <returns>仅由当前进程持有、不向子进程继承的作业句柄。</returns>
    /// <exception cref="Win32Exception">Windows 无法创建、配置或绑定作业对象。</exception>
    public static IntPtr AttachKillOnExit()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        var limits = new ExtendedLimits();
        limits.Basic.Flags = 0x00002000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        int length = Marshal.SizeOf(typeof(ExtendedLimits));
        IntPtr buffer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(limits, buffer, false);
            if (!SetInformationJobObject(job, 9, buffer, (uint)length))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            using (var process = Process.GetCurrentProcess())
            {
                if (!AssignProcessToJobObject(job, process.Handle))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            return job;
        }
        catch
        {
            CloseHandle(job);
            throw;
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }
}
