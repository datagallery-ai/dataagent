import { useEffect, useState } from 'react';

export function useTerminalSize(): { columns: number; rows: number } {
  const readSize = () => ({
    columns: process.stdout.columns || 80,
    rows: process.stdout.rows || 24,
  });
  const [size, setSize] = useState(readSize);

  useEffect(() => {
    const updateSize = () => {
      setSize(readSize());
    };

    process.stdout.on('resize', updateSize);
    return () => {
      process.stdout.off('resize', updateSize);
    };
  }, []);

  return size;
}
