// Offline draft only: compares categorical grades for a fixed blinded pair.
// No model identifiers, output content, or external side effects are accepted.
const fields = {done:['yes','partly','no'], reexplaining:['none','some','a lot'], speed:['yes','slow','too slow'], trust:['yes','somewhat','no']};
const sameKeys = (o, keys) => o && typeof o === 'object' && !Array.isArray(o) && Object.keys(o).sort().join('|') === [...keys].sort().join('|');
export function compareGrades(first, second) {
  const validate = record => {
    if (!sameKeys(record, ['case_id','responsibility','outputs']) || typeof record.case_id !== 'string' || !record.case_id.trim() || typeof record.responsibility !== 'boolean' || !sameKeys(record.outputs,['A','B'])) throw Error('invalid record');
    const dims = {...fields, ...(record.responsibility ? {follow_through:['yes','partly','no']} : {})};
    for (const label of ['A','B']) {
      if (!sameKeys(record.outputs[label],Object.keys(dims))) throw Error('invalid fields');
      for (const [key, values] of Object.entries(dims)) if (!values.includes(record.outputs[label][key])) throw Error('invalid answer');
    }
    return Object.keys(dims);
  };
  const dims=validate(first); validate(second);
  if (first.case_id !== second.case_id || first.responsibility !== second.responsibility) throw Error('mismatched case');
  const disagreements=[];
  for (const label of ['A','B']) for (const dimension of dims) if(first.outputs[label][dimension]!==second.outputs[label][dimension]) disagreements.push({label,dimension,first:first.outputs[label][dimension],second:second.outputs[label][dimension]});
  return {case_id:first.case_id, comparisons:2*dims.length, agreements:2*dims.length-disagreements.length, disagreements};
}
