import { useEffect, useState } from 'react';
import { useStdout } from 'ink';

export function useTerminalSize(): { columns: number; rows: number } {
  const { stdout } = useStdout();
  const readSize = () => ({
    columns: stdout.columns || 80,
    rows: stdout.rows || 24,
  });
  const [size, setSize] = useState(readSize);

  useEffect(() => {
    const updateSize = () => {
      setSize(readSize());
    };

    stdout.on('resize', updateSize);
    return () => {
      stdout.off('resize', updateSize);
    };
  }, [stdout]);

  return size;
}
