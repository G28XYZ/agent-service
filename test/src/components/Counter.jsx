import React, { useState } from 'react';

function Counter() {
  const [count, setCount] = useState(0);

  return (
    <div>
      <p>Count: {count}</p>
      <button onClick={() => setCount(count + 1)}>Increment</button>
      <button onClick={() => setCount(count - 1)}>Decrement</button>
    </div>
  );
}

export default Counter;
// Tests

import React from 'react';
import { render, screen } from '@testing-library/react';
import Counter from './Counter';


test('renders without crashing', () => {
  render(<Counter />);
  const element = screen.getByText(/count/i);
  expect(element).toBeInTheDocument();
});